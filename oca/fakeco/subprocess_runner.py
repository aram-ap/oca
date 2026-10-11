"""Real driftwood runner: OCA-owned sandbox, company production ops, git and Compose.

Argument vectors are fixed constants plus validated values; nothing here ever runs a
model-supplied string as a command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from oca.fakeco import diffcheck
from oca.fakeco.prometheus import PromClient, finite
from oca.fakeco.runner import (
    SERVICES, BackupRef, DeployRecord, GitCommit, PatchResult, RunnerError, SandboxInfo,
    StatusSnapshot, check_readable_path,
)
from oca.schemas import SandboxMetrics, TestRunResult
from oca.environments.compose import ComposeSandbox
from oca.environments.profile import load_profile

log = logging.getLogger("oca.runner")

PROJECT = "driftwood"
SERIES = ("orders_api:error_rate:1m", "orders_api:p95_latency_seconds:1m", "orders_api:rps:1m",
          "orders_api:db_pool_utilization")
GIT_IDENTITY = ("-c", "user.name=OCA Executor", "-c", "user.email=oca@localhost")
ENGINEER_IDENTITY = ("-c", "user.name=OCA Engineer", "-c", "user.email=oca@localhost")
MAX_FILE_BYTES = 64_000


class SubprocessRunner:
    mode = "real"

    def __init__(self, root: Path, prom_url: str, sandbox_prom_url: str, sandbox_api_url: str,
                 service_urls: dict[str, str], loadtest_rate: int = 30,
                 data_dir: Path = Path("data"), sandbox_profile: Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.prom = PromClient(prom_url)
        self.sandbox_prom = PromClient(sandbox_prom_url)
        self.sandbox_api_url = sandbox_api_url
        self.service_urls = service_urls
        self.loadtest_rate = loadtest_rate
        self.calls: list[dict[str, Any]] = []
        self._mutate = asyncio.Lock()
        if not (self.root / "ops" / "deploy.sh").exists():
            raise RunnerError(f"{self.root} does not look like a driftwood clone (ops/deploy.sh missing)")
        self.sandbox = ComposeSandbox(self.root, data_dir, load_profile(sandbox_profile),
                                      sandbox_api_url, sandbox_prom_url, self._sandbox_run)
        self.worktree = self.sandbox.worktree

    async def _sandbox_run(self, *argv: str, **kwargs):
        return await self._run(*argv, **kwargs)

    # ------------------------------------------------------------ plumbing

    async def _run(self, *argv: str, cwd: Path | None = None, timeout: float = 120, env: dict[str, str] | None = None,
                   stdin: str | None = None, check: bool = True, inherit_env: bool = True,
                   record_output: bool = True) -> tuple[int, str, str]:
        full_env = {**(os.environ if inherit_env else {}), **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd or self.root), env=full_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            proc.kill()
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RunnerError(f"{argv[0]} timed out after {timeout}s", list(argv), None)
        stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
        self.calls.append({"argv": list(argv), "rc": proc.returncode,
                           "stdout_tail": stdout[-300:] if record_output else "[redacted]",
                           "stderr_tail": stderr[-500:] if record_output else "[redacted]"})
        del self.calls[:-200]
        if check and proc.returncode != 0:
            detail = stderr if record_output else "[output redacted]"
            raise RunnerError(f"{' '.join(argv[:3])} failed (rc={proc.returncode}): {detail.strip()[-800:]}",
                              list(argv), proc.returncode, detail)
        return proc.returncode, stdout, stderr

    @staticmethod
    def _last_json(text: str) -> dict[str, Any]:
        """Parse the final JSON object in a stdout that may also contain progress lines."""
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        depth, start = 0, None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
        try:
            return json.loads(candidate)  # type: ignore[possibly-undefined]
        except (NameError, json.JSONDecodeError) as exc:
            raise RunnerError(f"No JSON object found in command output; tail: {text[-400:]!r}") from exc

    # ------------------------------------------------------------- observe

    async def status(self, sandbox: bool = False) -> StatusSnapshot:
        argv = ["python3", "ops/status.py", "--json"]
        if sandbox:
            argv += ["--prom", self.sandbox_prom.base_url]
        _, out, _ = await self._run(*argv, timeout=30)
        return StatusSnapshot.from_json(self._last_json(out))

    async def health(self, service: str) -> tuple[bool, str]:
        base = self.service_urls.get(service)
        if not base:
            return True, "no HTTP endpoint configured"
        url = base + ("/api/health" if service == "frontend" else "/health")
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                response = await client.get(url)
            return response.status_code < 300, f"HTTP {response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def metrics(self, service: str, sandbox: bool = False) -> dict[str, float]:
        prom = self.sandbox_prom if sandbox else self.prom
        out: dict[str, float] = {}
        for series in SERIES:
            value = await prom.scalar(series)
            if value is not None:
                out[series] = value
        for row in await prom.instant("orders_api:db_query_p95_seconds:1m"):
            value = finite(row.get("value", [None, None])[1])
            if value is not None:
                out[f'orders_api:db_query_p95_seconds:1m{{query="{row.get("metric", {}).get("query", "?")}"}}'] = value
        for row in await prom.instant("orders_api:db_errors:rate1m"):
            m, value = row.get("metric", {}), finite(row.get("value", [None, None])[1])
            if value is not None:
                out[f'orders_api:db_errors:rate1m{{kind="{m.get("kind", "?")}",query="{m.get("query", "?")}"}}'] = value
        return out

    async def containers(self) -> list[dict[str, Any]]:
        _, out, _ = await self._run("docker", "compose", "-p", PROJECT, "ps", "--all", "--format", "json", timeout=30)
        text = out.strip()
        if not text:
            return []
        if text.startswith("["):
            return list(json.loads(text))
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    async def read_logs(self, service: str, lines: int = 200) -> list[dict[str, Any]]:
        if service not in SERVICES:
            raise RunnerError(f"Unknown service {service!r}")
        lines = max(1, min(int(lines), 2000))
        if service in ("orders-api", "frontend"):
            path = self.root / "logs" / f"{service}.log"
            if not path.exists():
                return []
            _, out, _ = await self._run("tail", "-n", str(lines), str(path), timeout=15)
            rows = []
            for line in out.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"msg": line})
            return rows
        _, out, err = await self._run("docker", "compose", "-p", PROJECT, "logs", "--no-color", "--no-log-prefix",
                                      "--tail", str(lines), service, timeout=30)
        return [{"service": service, "msg": line} for line in (out + err).splitlines() if line.strip()]

    async def deployments(self, limit: int = 20) -> list[DeployRecord]:
        path = self.root / "state" / "deployments.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(DeployRecord.from_json(json.loads(line)))
                except (json.JSONDecodeError, ValueError):
                    continue
        return list(reversed(records))[:limit]

    async def git_log(self, ref: str = "main", n: int = 20) -> list[GitCommit]:
        _, out, _ = await self._run("git", "log", f"-n{int(n)}", "--format=%h%x1f%an <%ae>%x1f%aI%x1f%s", ref, timeout=30)
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append(GitCommit(*parts))
        return commits

    async def git_diff(self, a: str, b: str) -> str:
        self._check_ref(a)
        self._check_ref(b)
        _, out, _ = await self._run("git", "diff", a, b, "--", timeout=60)
        return out[:200_000]

    @staticmethod
    def _check_ref(ref: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", ref) or ref.startswith("-"):
            raise RunnerError(f"Invalid git ref {ref!r}")

    async def read_file(self, rel_path: str) -> str:
        path = check_readable_path(rel_path)
        target = (self.root / path).resolve()
        if not str(target).startswith(str(self.root) + os.sep):
            raise PermissionError(rel_path)
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        data = target.read_bytes()[:MAX_FILE_BYTES]
        return data.decode("utf-8", errors="replace")

    async def prod_sha(self) -> str:
        """The sha production is *running* (from the service's own app_info), not merely the last deploy record.

        The two differ when someone rebuilds by hand or runs ops/reset.sh without a redeploy; the
        sandbox must reproduce what customers currently hit.
        """
        try:
            snap = await self.status()
            if snap.git_sha and snap.git_sha not in ("unknown", "dev"):
                rc, out, _ = await self._run("git", "rev-parse", "--verify", "-q", f"{snap.git_sha}^{{commit}}",
                                             check=False, timeout=15)
                if rc == 0 and out.strip():
                    return snap.git_sha
        except (RunnerError, Exception):  # noqa: BLE001 - fall back to the deploy history
            pass
        records = await self.deployments(limit=1)
        if records:
            return records[0].git_sha
        _, out, _ = await self._run("git", "rev-parse", "--short=10", "HEAD", timeout=15)
        return out.strip()

    # ------------------------------------------------------------- sandbox

    async def sandbox_up(self, ref: str | None = None) -> SandboxInfo:
        ref = ref or await self.prod_sha()
        self._check_ref(ref)
        async with self._mutate:
            return await self.sandbox.up(ref)

    async def sandbox_loadtest(self, n: int) -> SandboxMetrics:
        n = max(1, min(int(n), 10000))
        _, out, _ = await self.sandbox.loadtest(n, self.loadtest_rate)
        data = self._last_json(out)
        if not isinstance(data.get("requests"), int) or data["requests"] <= 0:
            raise RunnerError("Sandbox load test returned no measured requests")
        return SandboxMetrics(**{k: data.get(k, 0) for k in ("requests", "failures", "error_rate_pct", "rps", "p50_ms",
                                                                "p95_ms", "p99_ms")},
                              statuses={str(k): int(v) for k, v in (data.get("statuses") or {}).items()},
                              by_endpoint=dict(data.get("by_endpoint") or {}))

    async def sandbox_tests(self) -> TestRunResult:
        rc, out, err = await self.sandbox.tests()
        text = out + err
        passed = len(re.findall(r"(\d+) passed", text)) and int(re.search(r"(\d+) passed", text).group(1)) or 0
        failed_match = re.search(r"(\d+) failed", text)
        failed = int(failed_match.group(1)) if failed_match else 0
        return TestRunResult(passed=rc == 0 and passed > 0, total=passed + failed, failed=failed, output_tail=text[-2000:],
                             command=f"docker compose -p {self.sandbox.project} run --rm oca-tests python -m pytest "
                                     + self.sandbox.profile.test_path)

    async def sandbox_reset_db(self) -> None:
        async with self._mutate:
            await self.sandbox.reset_db()

    async def sandbox_down(self) -> None:
        async with self._mutate:
            await self.sandbox.down()

    async def apply_patch_in_sandbox(self, branch: str, diff: str, message: str) -> PatchResult:
        self._check_ref(branch)
        if not branch.startswith("fix/"):
            raise RunnerError("Patch branches must be named fix/<ticket>")
        if not self.worktree.is_dir():
            raise RunnerError("Sandbox worktree is missing; run sandbox_up first")
        diff = diffcheck.normalize(diff)
        try:
            diffcheck.validate(diff)
        except ValueError as exc:
            return PatchResult(applied=False, branch=branch, error=f"rejected: {exc}")
        wt = self.worktree
        async with self._mutate:
            rc, _, _ = await self._run("git", "-C", str(wt), "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
                                       check=False, timeout=15)
            if rc == 0:
                await self._run("git", "-C", str(wt), "checkout", "-q", branch, timeout=60)
            else:
                await self._run("git", "-C", str(wt), "checkout", "-q", "-B", branch, timeout=60)
            applied_with, errors = None, []
            # Models get hunk arithmetic and path prefixes wrong far more often than the change itself:
            # escalate tolerance, trying the prefix level the headers suggest first.
            levels = ("-p1", "-p0") if re.search(r"^(---|\+\+\+) [ab]/", diff, re.M) else ("-p0", "-p1")
            variants = ([], ["--recount"], ["--recount", "--ignore-whitespace"],
                        ["--recount", "--ignore-whitespace", "--unidiff-zero"])
            for level in levels:
                for flags in variants:
                    argv = ["git", "-C", str(wt), "apply", level, "--whitespace=nowarn", *flags]
                    rc, _, err = await self._run(*argv, "--check", "-", stdin=diff, check=False, timeout=60)
                    if rc == 0:
                        await self._run(*argv, "-", stdin=diff, timeout=60)
                        applied_with = f"git apply {level} {' '.join(flags)}".strip()
                        break
                    errors.append(f"git apply {level} {' '.join(flags)}: {err.strip().splitlines()[-1][:120] if err.strip() else 'failed'}")
                if applied_with:
                    break
            if applied_with is None:
                # Last resort: GNU patch tolerates offsets and fuzzy context; a dry run must succeed first.
                for level in levels:
                    try:
                        rc, _, err = await self._run("patch", level, "--dry-run", "-f", "--fuzz=3", "--forward", "-d", str(wt),
                                                     stdin=diff, check=False, timeout=60)
                    except FileNotFoundError:
                        errors.append("GNU patch is unavailable; git apply attempts failed")
                        break
                    if rc == 0:
                        await self._run("patch", level, "-f", "--fuzz=3", "--forward", "--no-backup-if-mismatch", "-d", str(wt),
                                        stdin=diff, timeout=60)
                        applied_with = f"patch {level} --fuzz=3"
                        break
                    errors.append(f"patch {level} --fuzz=3: {err.strip().splitlines()[-1][:120] if err.strip() else 'failed'}")
            if applied_with is None:
                unique = list(dict.fromkeys(errors))
                return PatchResult(applied=False, branch=branch, error="patch does not apply: " + " | ".join(unique[-4:]))
            await self._run("git", "-C", str(wt), "add", "-A", "services", timeout=30)
            await self._run("git", "-C", str(wt), *ENGINEER_IDENTITY, "commit", "-q", "-m", message, timeout=30)
            _, sha, _ = await self._run("git", "-C", str(wt), "rev-parse", "--short=10", "HEAD", timeout=15)
            _, files, _ = await self._run("git", "-C", str(wt), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", timeout=15)
            _, stat, _ = await self._run("git", "-C", str(wt), "show", "--stat", "--format=", "HEAD", timeout=15)
        return PatchResult(applied=True, branch=branch, commit_sha=sha.strip(), files_changed=files.split(),
                           stat=(stat.strip() + f"\n(applied with {applied_with})"))

    async def sandbox_diff(self, branch: str) -> str:
        self._check_ref(branch)
        _, base, _ = await self._run("git", "merge-base", "main", branch, timeout=15)
        return await self.git_diff(base.strip(), branch)

    # ---------------------------------------------------- change production

    async def _assert_clean(self) -> None:
        _, out, _ = await self._run("git", "status", "--porcelain", "--", "services", "db", "prometheus",
                                    "docker-compose.yml", timeout=15)
        if out.strip():
            raise RunnerError("Production checkout is dirty; refusing to deploy:\n" + out)

    async def _deploy(self, note: str) -> DeployRecord:
        await self._run("ops/deploy.sh", note, timeout=900)
        records = await self.deployments(limit=1)
        if not records:
            raise RunnerError("deploy.sh produced no deployment record")
        return records[0]

    async def deploy_branch(self, branch: str, note: str) -> DeployRecord:
        self._check_ref(branch)
        async with self._mutate:
            await self._assert_clean()
            await self._run("git", "checkout", "-q", "main", timeout=30)
            await self._run("git", *GIT_IDENTITY, "merge", "--no-ff", "--no-edit", branch, timeout=60)
            return await self._deploy(note)

    async def set_config_and_deploy(self, key: str, value: int, note: str) -> DeployRecord:
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            raise RunnerError(f"Invalid config key {key!r}")
        path = self.root / "services" / "orders-api" / "config" / "app.env"
        async with self._mutate:
            await self._assert_clean()
            await self._run("git", "checkout", "-q", "main", timeout=30)
            text = path.read_text(encoding="utf-8")
            new, count = re.subn(rf"(?m)^{key}=.*$", f"{key}={int(value)}", text)
            if count == 0:
                new = text.rstrip("\n") + f"\n{key}={int(value)}\n"
            path.write_text(new, encoding="utf-8")
            await self._run("git", *GIT_IDENTITY, "commit", "-q", "-am", f"set {key}={int(value)} ({note})", timeout=30)
            return await self._deploy(note)

    async def restart_service(self, service: str) -> None:
        if service not in SERVICES:
            raise RunnerError(f"Unknown service {service!r}")
        async with self._mutate:
            await self._run("docker", "compose", "-p", PROJECT, "restart", service, timeout=300)

    async def backup(self, tag: str) -> BackupRef:
        self._check_ref(tag)
        sha = await self.prod_sha()
        async with self._mutate:
            await self._run("git", "tag", "-f", tag, sha, timeout=15)
        return BackupRef(tag=tag, sha=sha)

    async def restore(self, tag: str, note: str) -> DeployRecord:
        self._check_ref(tag)
        async with self._mutate:
            await self._run("git", "checkout", "-q", "main", timeout=30)
            await self._run("git", "reset", "-q", "--hard", tag, timeout=30)
            return await self._deploy(note)
