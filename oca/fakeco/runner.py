"""The seam between OCA and driftwood: a Protocol plus the result types it returns.

Two implementations exist: ``SubprocessRunner`` (real driftwood clone, docker, git)
and ``FakeRunner`` (deterministic, in-memory). Nothing else in the platform may
shell out or touch the fakeco checkout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from oca.schemas import SandboxMetrics, TestRunResult

SERVICES = ("orders-api", "frontend", "postgres", "redis")
PATCHABLE_ROOTS = ("services/orders-api/", "services/frontend/")
FORBIDDEN_PATCH_FILES = ("Dockerfile", "requirements.txt")
READABLE_ROOTS = ("services/", "prometheus/", "db/", "docker-compose.yml", "sandbox.env", "ops/")


class RunnerError(RuntimeError):
    def __init__(self, message: str, cmd: list[str] | None = None, rc: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.cmd, self.rc, self.stderr = cmd, rc, stderr


@dataclass
class StatusSnapshot:
    """``ops/status.py --json`` as a typed object."""

    ts: str
    rps: float
    error_rate_pct: float
    p95_latency_ms: float
    db_pool_utilization_pct: float
    db_errors_per_s: float
    version: str
    git_sha: str
    slowest_queries_p95_ms: dict[str, float] = field(default_factory=dict)
    db_errors: dict[str, float] = field(default_factory=dict)
    breaches: list[str] = field(default_factory=list)
    healthy: bool = True

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StatusSnapshot":
        def num(key: str) -> float:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        return cls(ts=str(data.get("ts", "")), rps=num("rps"), error_rate_pct=num("error_rate_pct"),
                   p95_latency_ms=num("p95_latency_ms"), db_pool_utilization_pct=num("db_pool_utilization_pct"),
                   db_errors_per_s=num("db_errors_per_s"), version=str(data.get("version", "")),
                   git_sha=str(data.get("git_sha", "")),
                   slowest_queries_p95_ms=dict(data.get("slowest_queries_p95_ms") or {}),
                   db_errors=dict(data.get("db_errors") or {}), breaches=list(data.get("breaches") or []),
                   healthy=bool(data.get("healthy", False)))

    def metrics(self) -> dict[str, float]:
        return {"error_rate_pct": self.error_rate_pct, "p95_latency_ms": self.p95_latency_ms,
                "db_pool_utilization_pct": self.db_pool_utilization_pct, "db_errors_per_s": self.db_errors_per_s,
                "rps": self.rps}

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, **self.metrics(), "version": self.version, "git_sha": self.git_sha,
                "slowest_queries_p95_ms": self.slowest_queries_p95_ms, "db_errors": self.db_errors,
                "breaches": self.breaches, "healthy": self.healthy}


@dataclass
class DeployRecord:
    """One line of driftwood's ``state/deployments.jsonl``."""

    deploy_id: int
    ts: str
    git_sha: str
    prev_sha: str | None
    version: str
    author: str
    subject: str
    files_changed: list[str] = field(default_factory=list)
    note: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "DeployRecord":
        return cls(deploy_id=int(data.get("deploy_id", 0)), ts=str(data.get("ts", "")), git_sha=str(data.get("git_sha", "")),
                   prev_sha=data.get("prev_sha"), version=str(data.get("version", "")), author=str(data.get("author", "")),
                   subject=str(data.get("subject", "")), files_changed=list(data.get("files_changed") or []),
                   note=data.get("note"))

    def as_dict(self) -> dict[str, Any]:
        return {"deploy_id": self.deploy_id, "ts": self.ts, "git_sha": self.git_sha, "prev_sha": self.prev_sha,
                "version": self.version, "author": self.author, "subject": self.subject,
                "files_changed": self.files_changed, "note": self.note}


@dataclass
class GitCommit:
    sha: str
    author: str
    date: str
    subject: str

    def as_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "author": self.author, "date": self.date, "subject": self.subject}


@dataclass
class SandboxInfo:
    git_sha: str
    version: str
    ref: str | None


@dataclass
class PatchResult:
    applied: bool
    branch: str | None = None
    commit_sha: str | None = None
    files_changed: list[str] = field(default_factory=list)
    stat: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "branch": self.branch, "commit_sha": self.commit_sha,
                "files_changed": self.files_changed, "stat": self.stat, "error": self.error}


@dataclass
class BackupRef:
    tag: str
    sha: str
    path: str | None = None


class FakecoRunner(Protocol):
    mode: str

    # -- observe production
    async def status(self, sandbox: bool = False) -> StatusSnapshot: ...
    async def health(self, service: str) -> tuple[bool, str]: ...
    async def metrics(self, service: str, sandbox: bool = False) -> dict[str, float]: ...
    async def containers(self) -> list[dict[str, Any]]: ...
    async def read_logs(self, service: str, lines: int = 200) -> list[dict[str, Any]]: ...
    async def deployments(self, limit: int = 20) -> list[DeployRecord]: ...
    async def git_log(self, ref: str = "main", n: int = 20) -> list[GitCommit]: ...
    async def git_diff(self, a: str, b: str) -> str: ...
    async def read_file(self, rel_path: str) -> str: ...
    async def read_sandbox_file(self, rel_path: str) -> str: ...   # the fix branch's view of a file
    async def list_files(self, rel_dir: str) -> list[str]: ...      # repository-relative paths under a readable dir
    async def prod_sha(self) -> str: ...

    # -- sandbox (proof loop)
    async def sandbox_up(self, ref: str | None = None) -> SandboxInfo: ...
    async def sandbox_loadtest(self, n: int) -> SandboxMetrics: ...
    async def sandbox_tests(self) -> TestRunResult: ...
    async def sandbox_reset_db(self) -> None: ...
    async def sandbox_down(self) -> None: ...
    async def apply_patch_in_sandbox(self, branch: str, diff: str, message: str) -> PatchResult: ...
    async def sandbox_diff(self, branch: str) -> str: ...

    # -- change production (executor only)
    async def deploy_branch(self, branch: str, note: str) -> DeployRecord: ...
    async def set_config_and_deploy(self, key: str, value: int, note: str) -> DeployRecord: ...
    async def restart_service(self, service: str) -> None: ...
    async def backup(self, tag: str) -> BackupRef: ...
    async def restore(self, tag: str, note: str) -> DeployRecord: ...


def check_readable_path(rel_path: str) -> str:
    """Reject traversal and anything outside the readable roots. Returns the normalised path."""
    import posixpath

    normalised = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
    if normalised.startswith("..") or "/../" in f"/{normalised}/" or normalised in {"", "."}:
        raise PermissionError(f"Path {rel_path!r} is outside the repository")
    if not any(normalised == root.rstrip("/") or normalised.startswith(root) for root in READABLE_ROOTS):
        raise PermissionError(f"Path {rel_path!r} is not under a readable root {READABLE_ROOTS}")
    return normalised
