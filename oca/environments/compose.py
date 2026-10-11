"""Build isolated Compose environments from company deployment files.

The resolved model is rewritten, then saved as a standalone file: production
ports, named resources and writable mounts are never inherited through a merge.
Only the operator profile chooses environment overrides and test locations.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
from urllib.parse import urlsplit

import yaml

from oca.environments.profile import ComposeProfile
from oca.fakeco.runner import RunnerError, SandboxInfo

log = logging.getLogger("oca.environments")
TEST_IMAGE_CONTEXT = Path(__file__).parent / "test_runner"
PUBLISHED_NETWORK = "oca-published"  # non-internal bridge for the loopback-published services only
SERVICE_KEYS = {
    "build", "image", "command", "entrypoint", "environment", "ports", "networks", "volumes",
    "depends_on", "healthcheck", "restart", "shm_size", "deploy", "labels", "expose", "profiles",
    "working_dir", "init", "user", "read_only", "tmpfs", "stop_grace_period", "stop_signal",
    "mem_limit", "cpus", "pids_limit", "pull_policy", "hostname", "logging",
}
HOST_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                 "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}


def loopback_port(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.port:
        raise RunnerError("Sandbox endpoints must be HTTP loopback URLs with explicit ports")
    if parsed.path not in {"", "/"} or parsed.username or parsed.query or parsed.fragment:
        raise RunnerError("Sandbox endpoints must be base URLs")
    return parsed.port


class ComposeSandbox:
    def __init__(self, root: Path, data_dir: Path, profile: ComposeProfile,
                 api_url: str, prom_url: str, run) -> None:
        self.root = root.resolve()
        self.profile = profile
        identity = hashlib.sha256(f"{self.root}|{data_dir.resolve()}".encode()).hexdigest()[:12]
        self.project = f"oca-sandbox-{profile.name}-{identity}"
        self.runtime = data_dir.resolve() / "sandboxes" / identity
        self.worktree = self.runtime / "checkout"
        self.model_path = self.runtime / "compose.json"
        self.marker = self.runtime / "owner.json"
        self.api_url = api_url
        self.ports = {profile.api_service: (profile.api_target_port, loopback_port(api_url)),
                      profile.prometheus_service: (profile.prometheus_target_port, loopback_port(prom_url)),
                      profile.frontend_service: (80, profile.frontend_port)}
        if len({port for _, port in self.ports.values()}) != len(self.ports):
            raise RunnerError("Sandbox published ports must be distinct")
        self._run = run
        self._check_location()

    def _check_location(self) -> None:
        if self.runtime == self.root or self.runtime.is_relative_to(self.root) or self.root.is_relative_to(self.runtime):
            raise RunnerError("Sandbox runtime must be outside the company checkout")
        for path in (self.runtime, self.worktree, self.marker, self.model_path,
                     self.runtime / "logs", self.runtime / "compose.env"):
            if path.resolve() != path or path.is_symlink():
                raise RunnerError("Sandbox runtime paths must not be symlinks or junctions")

    def _owned(self) -> dict:
        self._check_location()
        try:
            owner = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunnerError("Sandbox ownership record is missing or invalid") from exc
        if owner.get("root") != str(self.root) or owner.get("project") != self.project:
            raise RunnerError("Sandbox belongs to a different company or profile")
        return owner

    def _write(self, path: Path, value: dict) -> None:
        if path.is_symlink():
            raise RunnerError(f"Refusing to overwrite symlink: {path.name}")
        text = json.dumps(value, indent=2)
        # Compose interpolates even a resolved standalone file. Preserve literal
        # dollars in commands/environment rather than resolving them a second time.
        if path == self.model_path:
            text = text.replace("$", "$$")
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def _checkout_path(self, path: str) -> Path:
        result = (self.worktree / path).resolve()
        if not result.is_relative_to(self.worktree):
            raise RunnerError("Sandbox input path escapes its checkout")
        return result

    async def _command(self, *argv: str, **kwargs):
        # Do not inherit company .env values, proxy settings or agent/API tokens.
        env = {key: value for key, value in os.environ.items() if key.upper() in HOST_ENV_KEYS}
        env.update(kwargs.pop("env", {}))
        # Cleanup must still work if the checkout was removed independently.
        cwd = self.worktree if self.worktree.exists() else self.runtime
        return await self._run(*argv, cwd=cwd, env=env, inherit_env=False, **kwargs)

    def _compose_argv(self, path: Path) -> list[str]:
        return ["docker", "compose", "-p", self.project, "--project-directory", str(self.worktree),
                "--env-file", str(self.runtime / "compose.env"), "-f", str(path)]

    async def compose(self, *args: str, **kwargs):
        self._owned()
        if not self.model_path.is_file():
            raise RunnerError("Sandbox has not been prepared")
        return await self._command(*self._compose_argv(self.model_path), *args, **kwargs)

    def isolate(self, original: dict, password: str) -> dict:
        """Validate and rewrite the canonical Compose model before anything runs."""
        model = copy.deepcopy(original)
        if set(model) - {"name", "services", "volumes", "networks", "version"}:
            raise RunnerError("Unsupported Compose top-level settings (including secrets/configs)")
        services = model.get("services") or {}
        required = {self.profile.api_service, self.profile.database_service, self.profile.cache_service,
                    self.profile.prometheus_service, self.profile.frontend_service, self.profile.loadtest_service}
        if not required.issubset(services) or "oca-tests" in services:
            raise RunnerError("Compose services do not match the company profile")
        for kind in ("volumes", "networks"):
            resources = model.setdefault(kind, {})
            for name, resource in resources.items():
                resource = resource or {}
                driver = "local" if kind == "volumes" else "bridge"
                if resource.get("external") or resource.get("driver_opts") or resource.get("driver", driver) != driver:
                    raise RunnerError(f"Unsupported or external {kind}: {name}")
                resources[name] = {"name": f"{self.project}_{name}"}
                if kind == "networks":
                    resources[name].update(driver="bridge", internal=True)
        model["networks"].setdefault("default", {"name": f"{self.project}_default", "driver": "bridge", "internal": True})
        # Docker never binds host ports for containers that sit only on internal networks, so the
        # services OCA publishes on loopback also join a second, non-internal bridge. Everything
        # else (database, cache, load generator, tests) stays on the internal network only.
        if PUBLISHED_NETWORK in model["networks"]:
            raise RunnerError(f"Compose network name {PUBLISHED_NETWORK!r} is reserved by OCA")
        model["networks"][PUBLISHED_NETWORK] = {"name": f"{self.project}_{PUBLISHED_NETWORK}", "driver": "bridge"}
        for name, service in services.items():
            unexpected = set(service) - SERVICE_KEYS
            if unexpected:
                raise RunnerError(f"Unsupported Compose settings for {name}: {sorted(unexpected)}")
            if service.get("logging", {}).get("driver", "json-file") not in {"json-file", "local", "none"}:
                raise RunnerError("Sandbox logs cannot use an external logging driver")
            service.pop("logging", None)
            service.pop("labels", None)  # do not expose sandbox containers to production routers
            service.pop("profiles", None)
            service["restart"] = "no"
            service["deploy"] = {"replicas": 0 if name == self.profile.loadtest_service else 1}
            if name == self.profile.loadtest_service:
                service["profiles"] = ["oca-loadtesting"]
            service["networks"] = {"default": {}}
            service["ports"] = []
            if name in self.ports:
                target, published = self.ports[name]
                service["ports"] = [{"target": target, "published": str(published), "host_ip": "127.0.0.1", "protocol": "tcp"}]
                service["networks"][PUBLISHED_NETWORK] = {}
            allowed = self.profile.environment_allowlist.get(name, [])
            environment = service.get("environment") or {}
            service["environment"] = {key: value for key, value in environment.items() if key in allowed}
            service["environment"].update({key: value.replace("{db_password}", password)
                                            for key, value in self.profile.environment_overrides.get(name, {}).items()})
            build = service.get("build")
            if build:
                if set(build) - {"context", "dockerfile", "args", "target"}:
                    raise RunnerError(f"Unsupported build settings for {name}")
                context = self._checkout_path(build["context"])
                dockerfile = (context / build.get("dockerfile", "Dockerfile")).resolve()
                if not dockerfile.is_relative_to(context):
                    raise RunnerError("Dockerfile escapes its build context")
                build["context"] = str(context)
                service["image"] = f"{self.project}-{name}"
            for mount in service.get("volumes", []):
                kind = mount.get("type")
                if kind == "volume":
                    if mount.get("source") not in model["volumes"] or mount.get("volume", {}).get("subpath"):
                        raise RunnerError("Unsupported sandbox volume")
                elif kind == "bind":
                    if mount["target"] in self.profile.log_targets:
                        mount["source"] = str(self.runtime / "logs")
                        mount["read_only"] = False
                    else:
                        mount["source"] = str(self._checkout_path(mount["source"]))
                        mount["read_only"] = True
                    mount["bind"] = {"create_host_path": False}
                elif kind != "tmpfs":
                    raise RunnerError("Unsupported sandbox mount type")
        # Run the patched checkout's tests inside a container, not on the OCA host.
        services["oca-tests"] = {
            "build": {"context": str(TEST_IMAGE_CONTEXT.resolve())}, "image": "oca-sandbox-tests:1",
            "profiles": ["oca-testing"], "working_dir": "/workspace", "read_only": True,
            "tmpfs": ["/tmp"],
            "networks": {"default": {}},
            "environment": {"API_URL": f"http://{self.profile.api_service}:{self.profile.api_target_port}",
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            "volumes": [{"type": "bind", "source": str(self.worktree), "target": "/workspace", "read_only": True}],
        }
        model["name"] = self.project
        model.pop("version", None)
        return model

    async def up(self, ref: str) -> SandboxInfo:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", ref) or ref.startswith("-"):
            raise RunnerError("Invalid sandbox git ref")
        self._check_location()
        if self.runtime.exists():
            owner = self._owned()
        else:
            self.runtime.mkdir(parents=True, mode=0o700)
            owner = {"root": str(self.root), "project": self.project, "password": secrets.token_hex(24)}
            self._write(self.marker, owner)
        (self.runtime / "logs").mkdir(exist_ok=True)
        env_file = self.runtime / "compose.env"
        if env_file.is_symlink():
            raise RunnerError("Compose environment file must not be a symlink")
        env_file.write_text("# OCA deliberately does not load the company .env\n", encoding="utf-8")
        try:
            _, sha, _ = await self._run("git", "rev-parse", "--verify", f"{ref}^{{commit}}", timeout=30)
            sha = sha.strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise RunnerError("Git did not resolve the requested sandbox commit")
            if self.worktree.exists():
                await self._run("git", "-C", str(self.worktree), "checkout", "-q", "--detach", sha, timeout=60)
            else:
                await self._run("git", "worktree", "add", "-q", "--detach", str(self.worktree), sha, timeout=60)
            source = self._checkout_path(self.profile.compose_file)
            # Includes/extends could read files outside the checkout before resolution.
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) - {"name", "version", "services", "volumes", "networks"}:
                raise RunnerError("Unsupported company Compose document")
            for service in raw.get("services", {}).values():
                if "extends" in service:
                    raise RunnerError("Compose extends is not supported")
                files = service.get("env_file", [])
                for entry in ([files] if isinstance(files, str) else files):
                    self._checkout_path(entry if isinstance(entry, str) else entry["path"])
            version = self._checkout_path(self.profile.version_file).read_text(encoding="utf-8").strip()
            _, out, _ = await self._command(*self._compose_argv(source), "config", "--format", "json",
                                           env={**self.profile.compose_variables, "APP_VERSION": version, "GIT_SHA": sha},
                                           timeout=60, record_output=False)
            model = self.isolate(json.loads(out), owner["password"])
            self._write(self.model_path, model)
            await self.compose("up", "-d", "--build", "--wait", "--wait-timeout", "900", timeout=1200)
            await self.flush_cache()
            return SandboxInfo(git_sha=sha, version=version, ref=ref)
        except (Exception, asyncio.CancelledError):
            try:
                await asyncio.shield(self.down())
            except Exception:
                log.exception("Sandbox cleanup failed; ownership/configuration retained for retry")
            raise

    async def flush_cache(self) -> None:
        await self.compose("exec", "-T", self.profile.cache_service, "redis-cli", "FLUSHALL", timeout=30)

    async def loadtest(self, requests: int, rate: int):
        await self.compose("build", "-q", self.profile.loadtest_service, timeout=300)
        return await self.compose("run", "--rm", "-T", "--no-deps", self.profile.loadtest_service,
                                  f"--base-url={self.profile.loadtest_url}", f"--requests={requests}", f"--rate={rate}", timeout=900)

    async def tests(self):
        self._checkout_path(self.profile.test_path)
        await self.compose("build", "-q", "oca-tests", timeout=300)
        return await self.compose("run", "--rm", "-T", "--no-deps", "oca-tests", "python", "-m", "pytest",
                                  self.profile.test_path, "-q", "-p", "no:cacheprovider", timeout=600, check=False)

    async def reset_db(self) -> None:
        # Recreate the sandbox DB from the company's normal synthetic seed files.
        # No company reset script or production schema is involved.
        self._owned()
        ref = (await self._run("git", "-C", str(self.worktree), "rev-parse", "HEAD", timeout=30))[1].strip()
        await self.compose("--profile", "*", "down", "-v", "--remove-orphans", timeout=300)
        await self.up(ref)

    async def down(self) -> None:
        if not self.runtime.exists():
            return
        self._owned()  # checked absolute target and owner before any recursive removal
        if self.model_path.exists():
            await self.compose("--profile", "*", "down", "-v", "--remove-orphans", timeout=300)
        if self.worktree.exists():
            await self._run("git", "worktree", "remove", "--force", str(self.worktree), timeout=60)
        self._check_location()
        shutil.rmtree(self.runtime)
