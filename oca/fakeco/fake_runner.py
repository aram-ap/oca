"""Deterministic in-memory driftwood for development and tests (OCA_RUNNER=dry).

Models the two shipped incidents. ``timeout``: deployment #2 sets DB_TIMEOUT_MS to
100 and /bestsellers fails; a patch restoring DB_TIMEOUT_MS >= 500 fixes it.
``loyalty``: deployment #2 adds an unindexed query; a new migration file creating
an index on orders(customer_email) fixes it. Rolling back also "fixes" either.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from oca.fakeco import diffcheck
from oca.fakeco.runner import (
    SERVICES, BackupRef, DeployRecord, GitCommit, PatchResult, RunnerError, SandboxInfo,
    StatusSnapshot, check_readable_path,
)
from oca.schemas import SandboxMetrics, TestRunResult

BASELINE_SHA = "a1b2c3d4e5"
BASELINE_VERSION = "1.3.0"
BASE_CONFIG = {"DB_TIMEOUT_MS": 3000, "DB_POOL_MIN": 2, "DB_POOL_MAX": 10, "DB_POOL_ACQUIRE_TIMEOUT_MS": 2000,
               "PRODUCTS_CACHE_TTL_S": 60, "BESTSELLERS_CACHE_TTL_S": 30}

INCIDENTS: dict[str, dict[str, Any]] = {
    "timeout": {
        "sha": "f6e5d4c3b2", "version": "1.3.1", "author": "Priya Nair <priya@driftwood.coffee>",
        "subject": "Merge branch 'perf/fail-fast-db-timeouts'",
        "files_changed": ["services/orders-api/config/app.env", "services/orders-api/VERSION"],
        "config": {"DB_TIMEOUT_MS": 100},
        "status": {"rps": 128.0, "error_rate_pct": 25.3, "p95_latency_ms": 10234.0, "db_pool_utilization_pct": 62.0,
                   "db_errors_per_s": 7.1, "slowest_queries_p95_ms": {"bestsellers_30d": 100.0, "list_products": 3.1},
                   "db_errors": {"timeout:bestsellers_30d": 7.1}, "breaches": ["error_rate_pct", "p95_latency_ms"]},
        "loadtest": {"error_rate_pct": 24.8, "p50_ms": 23.0, "p95_ms": 10012.0, "p99_ms": 10040.0,
                     "by_endpoint": {"/bestsellers": {"requests": 500, "failures": 496, "error_rate_pct": 99.2},
                                     "/products": {"requests": 500, "failures": 0, "error_rate_pct": 0.0},
                                     "/session": {"requests": 500, "failures": 0, "error_rate_pct": 0.0}}},
        "diff": (
            "diff --git a/services/orders-api/config/app.env b/services/orders-api/config/app.env\n"
            "--- a/services/orders-api/config/app.env\n+++ b/services/orders-api/config/app.env\n"
            "@@ -1,6 +1,6 @@\n # orders-api runtime config. Deployed with the service: change it with a commit,\n"
            " # then run ops/deploy.sh.\n-DB_TIMEOUT_MS=3000\n+DB_TIMEOUT_MS=100\n DB_POOL_MIN=2\n DB_POOL_MAX=10\n"
            " DB_POOL_ACQUIRE_TIMEOUT_MS=2000\n"
            "diff --git a/services/orders-api/VERSION b/services/orders-api/VERSION\n"
            "--- a/services/orders-api/VERSION\n+++ b/services/orders-api/VERSION\n@@ -1 +1 @@\n-1.3.0\n+1.3.1\n"),
        "error_log": {"level": "ERROR", "msg": "database request timeout", "route": "/bestsellers", "status": 500,
                      "query": "bestsellers_30d", "db_timeout_ms": 100, "error_type": "DatabaseTimeout"},
    },
    "loyalty": {
        "sha": "0b1c2d3e4f", "version": "1.4.0", "author": "Marcus Bell <marcus@driftwood.coffee>",
        "subject": "Merge branch 'feature/loyalty-tiers'",
        "files_changed": ["services/orders-api/app/main.py", "services/orders-api/app/db.py",
                          "services/orders-api/tests/test_smoke.py", "services/frontend/html/index.html",
                          "services/orders-api/VERSION"],
        "config": {},
        "status": {"rps": 61.0, "error_rate_pct": 47.0, "p95_latency_ms": 15000.0, "db_pool_utilization_pct": 100.0,
                   "db_errors_per_s": 20.3,
                   "slowest_queries_p95_ms": {"loyalty_lifetime_orders": 1190.0, "bestsellers_30d": 2400.0},
                   "db_errors": {"pool_exhausted:acquire": 20.3},
                   "breaches": ["error_rate_pct", "p95_latency_ms", "db_pool_utilization_pct"]},
        "loadtest": {"error_rate_pct": 47.1, "p50_ms": 5500.0, "p95_ms": 15020.0, "p99_ms": 15200.0,
                     "by_endpoint": {"/session": {"requests": 500, "failures": 260, "error_rate_pct": 52.0},
                                     "/bestsellers": {"requests": 500, "failures": 240, "error_rate_pct": 48.0},
                                     "/products": {"requests": 500, "failures": 205, "error_rate_pct": 41.0}}},
        "diff": (
            "diff --git a/services/orders-api/app/db.py b/services/orders-api/app/db.py\n"
            "--- a/services/orders-api/app/db.py\n+++ b/services/orders-api/app/db.py\n"
            "@@ -88,6 +88,14 @@ async def get_order(order_id: int):\n"
            "+async def loyalty_lifetime_orders(email: str) -> int:\n"
            "+    row = await fetchrow(\n"
            "+        \"loyalty_lifetime_orders\",\n"
            "+        \"SELECT count(*) AS n, coalesce(sum(total_cents),0) AS cents FROM orders WHERE customer_email = $1\",\n"
            "+        email,\n+    )\n+    return tier_for(row[\"n\"], row[\"cents\"])\n+\n"
            "diff --git a/services/orders-api/app/main.py b/services/orders-api/app/main.py\n"
            "--- a/services/orders-api/app/main.py\n+++ b/services/orders-api/app/main.py\n"
            "@@ -140,6 +140,8 @@ async def session(email: str):\n"
            "+    tier = await db.loyalty_lifetime_orders(email)\n+    payload[\"tier\"] = tier\n"
            "diff --git a/services/orders-api/VERSION b/services/orders-api/VERSION\n"
            "--- a/services/orders-api/VERSION\n+++ b/services/orders-api/VERSION\n@@ -1 +1 @@\n-1.3.0\n+1.4.0\n"),
        "error_log": {"level": "ERROR", "msg": "database connection pool exhausted", "route": "/session", "status": 503,
                      "query": "acquire", "pool_max": 10, "acquire_timeout_ms": 2000, "error_type": "PoolExhausted"},
    },
}

HEALTHY_STATUS = {"rps": 135.0, "error_rate_pct": 0.0, "p95_latency_ms": 12.4, "db_pool_utilization_pct": 18.0,
                  "db_errors_per_s": 0.0, "slowest_queries_p95_ms": {"bestsellers_30d": 790.0, "list_products": 3.1},
                  "db_errors": {}, "breaches": []}
HEALTHY_LOADTEST = {"error_rate_pct": 0.0, "p50_ms": 5.6, "p95_ms": 16.0, "p99_ms": 31.0,
                    "by_endpoint": {"/bestsellers": {"requests": 500, "failures": 0, "error_rate_pct": 0.0},
                                    "/products": {"requests": 500, "failures": 0, "error_rate_pct": 0.0},
                                    "/session": {"requests": 500, "failures": 0, "error_rate_pct": 0.0}}}

APP_ENV_TEMPLATE = ("# orders-api runtime config. Deployed with the service: change it with a commit,\n"
                    "# then run ops/deploy.sh.\n{body}")
CONFIG_PY = '''"""orders-api settings, read from the environment (compose loads config/app.env)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ["DATABASE_URL"]
    redis_url: str = os.environ["REDIS_URL"]
    db_timeout_ms: int = int(os.getenv("DB_TIMEOUT_MS", "3000"))
    db_pool_min: int = int(os.getenv("DB_POOL_MIN", "2"))
    db_pool_max: int = int(os.getenv("DB_POOL_MAX", "10"))
    db_pool_acquire_timeout_ms: int = int(os.getenv("DB_POOL_ACQUIRE_TIMEOUT_MS", "2000"))
    products_cache_ttl_s: int = int(os.getenv("PRODUCTS_CACHE_TTL_S", "60"))
    bestsellers_cache_ttl_s: int = int(os.getenv("BESTSELLERS_CACHE_TTL_S", "30"))


settings = Settings()
'''
DB_PY = '''"""asyncpg pool with a per-pool command timeout; named queries feed db_query_duration_seconds."""
import asyncio
import asyncpg
from .config import settings
from . import telemetry

pool: asyncpg.Pool | None = None


async def connect():
    global pool
    pool = await asyncpg.create_pool(settings.database_url, min_size=settings.db_pool_min,
                                     max_size=settings.db_pool_max,
                                     command_timeout=settings.db_timeout_ms / 1000)
    await migrate()


async def fetch(query_name: str, sql: str, *args):
    async with acquire() as conn:
        with telemetry.db_timer(query_name):
            try:
                return await conn.fetch(sql, *args)
            except asyncio.TimeoutError as exc:
                telemetry.db_error("timeout", query_name)
                raise DatabaseTimeout(query_name, settings.db_timeout_ms) from exc
'''


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


class FakeRunner:
    mode = "dry"

    def __init__(self, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self.calls: list[str] = []
        self.failure_modes: set[str] = set()
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self.incident: str | None = None
        self.config: dict[str, int] = dict(BASE_CONFIG)
        self.version = BASELINE_VERSION
        self.sha = BASELINE_SHA
        self.worse = False
        self.migrations: set[str] = {"001_baseline.sql"}
        self.commits: list[GitCommit] = [GitCommit(BASELINE_SHA, "ops <ops@driftwood.coffee>",
                                                   (_now() - timedelta(days=3)).isoformat(), "Baseline v1.3.0")]
        self.deploy_log: list[DeployRecord] = [DeployRecord(1, (_now() - timedelta(days=3)).isoformat(), BASELINE_SHA,
                                                             None, BASELINE_VERSION, "ops", "Baseline v1.3.0",
                                                             [], "initial deploy")]
        self.history: dict[str, dict[str, Any]] = {BASELINE_SHA: self._snapshot()}
        self.branches: dict[str, dict[str, Any]] = {}
        self.sandbox: dict[str, Any] = {"up": False, "ref": None, "sha": None}
        self.tags: dict[str, str] = {}
        self.restarts: list[str] = []

    def _snapshot(self) -> dict[str, Any]:
        return {"incident": self.incident, "config": dict(self.config), "version": self.version,
                "migrations": set(self.migrations)}

    def _restore_snapshot(self, snap: dict[str, Any]) -> None:
        self.incident, self.config = snap["incident"], dict(snap["config"])
        self.version, self.migrations = snap["version"], set(snap["migrations"])

    def inject(self, name: str) -> DeployRecord:
        """What ``ops/inject.sh <name>`` does: merge the teammate's branch and deploy it."""
        spec = INCIDENTS[name]
        self.incident = name
        self.config.update(spec["config"])
        self.version = spec["version"]
        prev = self.sha
        self.sha = spec["sha"]
        self.commits.insert(0, GitCommit(self.sha, spec["author"], _now().isoformat(), spec["subject"]))
        record = DeployRecord(len(self.deploy_log) + 1, _now().isoformat(), self.sha, prev, self.version,
                              spec["author"], spec["subject"], list(spec["files_changed"]), None)
        self.deploy_log.append(record)
        self.history[self.sha] = self._snapshot()
        return record

    def _state_for(self, incident: str | None, config: dict[str, int], migrations: set[str]) -> str | None:
        """Which incident is still present given config and applied migrations."""
        if incident == "timeout" and config.get("DB_TIMEOUT_MS", 3000) >= 500:
            return None
        if incident == "loyalty" and any("customer_email" in m for m in migrations):
            return None
        return incident

    def _effective_incident(self) -> str | None:
        return self._state_for(self.incident, self.config, self.migrations)

    def _status_dict(self, incident: str | None, worse: bool = False) -> dict[str, Any]:
        base = dict(INCIDENTS[incident]["status"]) if incident else dict(HEALTHY_STATUS)
        if worse:  # a deploy that broke production harder than the incident itself
            base.update({"error_rate_pct": 85.0, "p95_latency_ms": 30000.0, "db_pool_utilization_pct": 100.0,
                         "db_errors_per_s": 40.0, "db_errors": {"pool_exhausted:acquire": 40.0},
                         "breaches": ["error_rate_pct", "p95_latency_ms", "db_pool_utilization_pct"]})
        jitter = self._rng.uniform(-0.02, 0.02)
        base["error_rate_pct"] = round(max(0.0, base["error_rate_pct"] * (1 + jitter)), 2)
        base["p95_latency_ms"] = round(base["p95_latency_ms"] * (1 + jitter), 1)
        base["healthy"] = not base["breaches"]
        return base

    def _branch_state(self, ref: str) -> tuple[str | None, dict[str, Any]]:
        if ref in self.branches:
            b = self.branches[ref]
            return self._state_for(b["incident"], b["config"], b["migrations"]), b
        snap = self.history.get(ref, self._snapshot())
        return self._state_for(snap["incident"], snap["config"], snap["migrations"]), snap

    # -------------------------------------------------------------- observe

    async def status(self, sandbox: bool = False) -> StatusSnapshot:
        self.calls.append(f"status sandbox={sandbox}")
        if sandbox:
            if not self.sandbox["up"]:
                raise RunnerError("sandbox is not running")
            incident, state = self._branch_state(self.sandbox["ref"] or self.sandbox["sha"])
            data = self._status_dict(incident)
            version, sha = state.get("version", self.version), self.sandbox["sha"]
        else:
            data = self._status_dict(self._effective_incident(), self.worse)
            version, sha = self.version, self.sha
        return StatusSnapshot.from_json({**data, "ts": _now().isoformat(), "version": version, "git_sha": sha})

    async def health(self, service: str) -> tuple[bool, str]:
        incident = self._effective_incident()
        if service in ("orders-api", "frontend") and incident == "loyalty":
            return False, "HTTP 503 {\"status\":\"degraded\",\"checks\":{\"postgres\":\"pool exhausted\"}}"
        return True, "HTTP 200 {\"status\":\"ok\"}"

    async def metrics(self, service: str, sandbox: bool = False) -> dict[str, float]:
        snap = await self.status(sandbox)
        series = {"orders_api:error_rate:1m": snap.error_rate_pct / 100, "orders_api:p95_latency_seconds:1m": snap.p95_latency_ms / 1000,
                  "orders_api:rps:1m": snap.rps, "orders_api:db_pool_utilization": snap.db_pool_utilization_pct / 100}
        for query, p95 in snap.slowest_queries_p95_ms.items():
            series[f'orders_api:db_query_p95_seconds:1m{{query="{query}"}}'] = p95 / 1000
        for kind_query, rate in snap.db_errors.items():
            kind, _, query = kind_query.partition(":")
            series[f'orders_api:db_errors:rate1m{{kind="{kind}",query="{query}"}}'] = rate
        return series

    async def containers(self) -> list[dict[str, Any]]:
        unhealthy = self._effective_incident() == "loyalty"
        rows = []
        for name, image, ports, health in [
            ("driftwood-orders-api-1", "driftwood-orders-api", "0.0.0.0:8000->8000/tcp", "unhealthy" if unhealthy else "healthy"),
            ("driftwood-frontend-1", "nginx:1.27-alpine", "0.0.0.0:8080->80/tcp", ""),
            ("driftwood-postgres-1", "postgres:16-alpine", "0.0.0.0:5432->5432/tcp", "healthy"),
            ("driftwood-redis-1", "redis:7-alpine", "0.0.0.0:6379->6379/tcp", "healthy"),
            ("driftwood-prometheus-1", "prom/prometheus:v3.1.0", "0.0.0.0:9090->9090/tcp", ""),
            ("driftwood-loadgen-1", "driftwood-loadgen", "", ""),
        ]:
            rows.append({"Name": name, "Image": image, "State": "running", "Status": "Up 2 hours" + (f" ({health})" if health else ""),
                         "Health": health, "Ports": ports, "RestartCount": 0})
        return rows

    async def read_logs(self, service: str, lines: int = 200) -> list[dict[str, Any]]:
        if service not in SERVICES:
            raise RunnerError(f"Unknown service {service!r}")
        incident = self._effective_incident()
        spec = INCIDENTS[incident] if incident else None
        rng = random.Random(f"{service}:{incident}:{lines}")
        out: list[dict[str, Any]] = []
        now = _now()
        routes = ["/products", "/bestsellers", "/session", "/cart", "/orders", "/products/3"]
        for i in range(lines):
            ts = (now - timedelta(milliseconds=(lines - i) * 180)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            rid = f"{rng.randrange(16**8):08x}"
            if service == "orders-api":
                route = rng.choice(routes)
                err_route = spec["error_log"]["route"] if spec else None
                failing = spec is not None and (route == err_route or (incident == "loyalty" and rng.random() < 0.35))
                if failing:
                    out.append({"ts": ts, "level": "ERROR", "service": "orders-api", "version": self.version, "request_id": rid,
                                "method": "GET", "route": route, "duration_ms": rng.choice([100.4, 100.9, 2001.2]),
                                **{k: v for k, v in spec["error_log"].items() if k not in ("route",)}})
                else:
                    out.append({"ts": ts, "level": "INFO", "service": "orders-api", "version": self.version, "request_id": rid,
                                "method": "POST" if route in ("/cart", "/orders") else "GET", "route": route,
                                "status": 201 if route == "/orders" else 200, "duration_ms": round(rng.uniform(2, 14), 1),
                                "msg": "request"})
            elif service == "frontend":
                route = rng.choice(routes)
                status = 500 if (spec and route == spec["error_log"]["route"]) else 200
                out.append({"ts": ts, "service": "frontend", "method": "GET", "path": "/api" + route, "status": status,
                            "duration_ms": round(rng.uniform(3, 20), 1), "upstream_ms": round(rng.uniform(2, 15), 1),
                            "request_id": rid})
            elif service == "postgres":
                msg = "LOG:  duration: 812.4 ms  statement: SELECT p.id ... FROM orders o JOIN order_items ..." \
                    if rng.random() < 0.3 else "LOG:  checkpoint complete"
                out.append({"ts": ts, "service": "postgres", "msg": msg})
            else:
                out.append({"ts": ts, "service": "redis", "msg": "1:M * DB saved on disk" if rng.random() < 0.1 else "1:M * Background saving started"})
        return out

    async def deployments(self, limit: int = 20) -> list[DeployRecord]:
        return list(reversed(self.deploy_log))[:limit]

    async def git_log(self, ref: str = "main", n: int = 20) -> list[GitCommit]:
        if ref in self.branches:
            return self.branches[ref]["commits"][:n]
        return self.commits[:n]

    async def git_diff(self, a: str, b: str) -> str:
        for name, spec in INCIDENTS.items():
            if spec["sha"].startswith(b.rstrip("~1").split("..")[-1][:10]) and len(b) >= 6:
                return spec["diff"]
        if b in self.branches:
            return "\n".join(self.branches[b]["diffs"])
        for record in self.deploy_log:
            if record.git_sha == b and record.note and record.note.startswith("INC-") and record.subject in self.branches:
                return "\n".join(self.branches[record.subject]["diffs"])
        return ""

    async def read_file(self, rel_path: str) -> str:
        path = check_readable_path(rel_path)
        files = self._files()
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def _files(self) -> dict[str, str]:
        env_body = "\n".join(f"{k}={v}" for k, v in self.config.items()) + "\n"
        files = {
            "services/orders-api/config/app.env": APP_ENV_TEMPLATE.format(body=env_body),
            "services/orders-api/VERSION": self.version + "\n",
            "services/orders-api/app/config.py": CONFIG_PY,
            "services/orders-api/app/db.py": DB_PY,
            "services/orders-api/migrations/001_baseline.sql": "SELECT 1;\n",
            "services/orders-api/requirements.txt": "fastapi==0.115.6\nuvicorn==0.34.0\nasyncpg==0.30.0\nredis==5.2.1\nprometheus-client==0.21.1\n",
        }
        return files

    async def prod_sha(self) -> str:
        return self.sha

    # -------------------------------------------------------------- sandbox

    async def sandbox_up(self, ref: str | None = None) -> SandboxInfo:
        self.calls.append(f"sandbox_up {ref}")
        if "sandbox_up_fails" in self.failure_modes:
            raise RunnerError("docker compose up failed", rc=1, stderr="orders-api: unhealthy")
        if ref is None or ref == self.sha:
            sha, version, ref = self.sha, self.version, None
        elif ref in self.branches:
            sha, version = self.branches[ref]["sha"], self.branches[ref]["version"]
        elif ref in self.history:
            sha, version = ref, self.history[ref]["version"]
        else:
            raise RunnerError(f"unknown ref {ref!r}", rc=128, stderr="fatal: bad revision")
        self.sandbox = {"up": True, "ref": ref, "sha": sha}
        return SandboxInfo(git_sha=sha, version=version, ref=ref)

    def _require_sandbox(self) -> None:
        if not self.sandbox["up"]:
            raise RunnerError("sandbox is not running; call sandbox_up first")

    async def sandbox_loadtest(self, n: int) -> SandboxMetrics:
        self._require_sandbox()
        self.calls.append(f"sandbox_loadtest {n}")
        incident, _ = self._branch_state(self.sandbox["ref"] or self.sandbox["sha"])
        base = dict(INCIDENTS[incident]["loadtest"]) if incident else dict(HEALTHY_LOADTEST)
        failures = int(round(n * base["error_rate_pct"] / 100))
        statuses = {"200": n - failures, "500": failures} if failures else {"200": n}
        return SandboxMetrics(requests=n, failures=failures, error_rate_pct=round(100 * failures / n, 2) if n else 0.0,
                              rps=29.6, p50_ms=base["p50_ms"], p95_ms=base["p95_ms"], p99_ms=base["p99_ms"],
                              statuses=statuses, by_endpoint=base["by_endpoint"])

    async def sandbox_tests(self) -> TestRunResult:
        self._require_sandbox()
        self.calls.append("sandbox_tests")
        incident, _ = self._branch_state(self.sandbox["ref"] or self.sandbox["sha"])
        if incident == "timeout":
            return TestRunResult(passed=False, total=7, failed=1, duration_s=4.2, command="python3 -m pytest services/orders-api/tests -q",
                                 output_tail="FAILED tests/test_smoke.py::test_bestsellers - assert 500 == 200\n6 passed, 1 failed")
        if incident == "loyalty":
            return TestRunResult(passed=False, total=7, failed=3, duration_s=31.0, command="python3 -m pytest services/orders-api/tests -q",
                                 output_tail="FAILED test_cart_and_session - httpx.ReadTimeout\nFAILED test_checkout_roundtrip\nFAILED test_bestsellers\n4 passed, 3 failed")
        return TestRunResult(passed=True, total=7, failed=0, duration_s=3.1, command="python3 -m pytest services/orders-api/tests -q",
                             output_tail="7 passed in 3.1s")

    async def sandbox_reset_db(self) -> None:
        self._require_sandbox()
        self.calls.append("sandbox_reset_db")

    async def sandbox_down(self) -> None:
        self.calls.append("sandbox_down")
        self.sandbox = {"up": False, "ref": None, "sha": None}

    async def apply_patch_in_sandbox(self, branch: str, diff: str, message: str) -> PatchResult:
        self._require_sandbox()
        self.calls.append(f"apply_patch {branch}")
        try:
            info = diffcheck.validate(diff)
        except ValueError as exc:
            return PatchResult(applied=False, branch=branch, error=f"git apply --check failed: {exc}")
        if branch not in self.branches:
            base_sha = self.sandbox["sha"]
            incident, state = self._branch_state(base_sha)
            snap = self.history.get(base_sha, self._snapshot())
            self.branches[branch] = {"base_sha": base_sha, "sha": base_sha, "incident": snap["incident"],
                                     "config": dict(snap["config"]), "migrations": set(snap["migrations"]),
                                     "version": snap["version"], "commits": list(self.commits), "diffs": [], "files": []}
        b = self.branches[branch]
        for key, value in info.config_changes.items():
            if value.lstrip("-").isdigit():
                b["config"][key] = int(value)
        for path, sql in info.migration_sql.items():
            if "create index" in sql.lower():
                b["migrations"].add(path.rsplit("/", 1)[-1] + ":" + sql.lower())
        b["diffs"].append(diff)
        b["files"] = sorted(set(b["files"]) | set(info.files))
        b["sha"] = _sha(branch, str(len(b["diffs"])), diff)
        b["commits"].insert(0, GitCommit(b["sha"], "OCA Engineer <oca@localhost>", _now().isoformat(), message))
        self.sandbox = {"up": True, "ref": branch, "sha": b["sha"]}
        stat = "\n".join(f" {f} | {info.added + info.removed} +-" for f in info.files)
        return PatchResult(applied=True, branch=branch, commit_sha=b["sha"], files_changed=list(info.files), stat=stat)

    async def sandbox_diff(self, branch: str) -> str:
        if branch not in self.branches:
            raise RunnerError(f"unknown branch {branch!r}")
        return "\n".join(self.branches[branch]["diffs"])

    # ------------------------------------------------------ change production

    def _record(self, subject: str, files: list[str], note: str, author: str = "OCA Executor <oca@localhost>") -> DeployRecord:
        prev = self.sha
        self.sha = _sha(prev, subject, note, str(len(self.deploy_log)))
        self.commits.insert(0, GitCommit(self.sha, author, _now().isoformat(), subject))
        record = DeployRecord(len(self.deploy_log) + 1, _now().isoformat(), self.sha, prev, self.version, author,
                              subject, files, note)
        self.deploy_log.append(record)
        self.history[self.sha] = self._snapshot()
        return record

    async def deploy_branch(self, branch: str, note: str) -> DeployRecord:
        self.calls.append(f"deploy_branch {branch}")
        if branch not in self.branches:
            raise RunnerError(f"merge: {branch} - not something we can merge", rc=1)
        if "deploy_fails" in self.failure_modes:
            raise RunnerError("ops/deploy.sh failed", rc=1, stderr="orders-api did not become healthy")
        b = self.branches[branch]
        self.config.update(b["config"])
        self.migrations |= b["migrations"]
        self.worse = "deploy_makes_worse" in self.failure_modes
        return self._record(branch, list(b["files"]), note)

    async def set_config_and_deploy(self, key: str, value: int, note: str) -> DeployRecord:
        self.calls.append(f"set_config {key}={value}")
        if "deploy_fails" in self.failure_modes:
            raise RunnerError("ops/deploy.sh failed", rc=1)
        self.config[key] = value
        self.worse = "deploy_makes_worse" in self.failure_modes
        return self._record(f"set {key}={value} ({note})", ["services/orders-api/config/app.env"], note)

    async def restart_service(self, service: str) -> None:
        self.calls.append(f"restart {service}")
        self.restarts.append(service)

    async def backup(self, tag: str) -> BackupRef:
        self.calls.append(f"backup {tag}")
        self.tags[tag] = self.sha
        return BackupRef(tag=tag, sha=self.sha)

    async def restore(self, tag: str, note: str) -> DeployRecord:
        self.calls.append(f"restore {tag}")
        if tag not in self.tags:
            raise RunnerError(f"unknown tag {tag!r}", rc=128)
        target = self.tags[tag]
        self._restore_snapshot(self.history[target])
        self.worse = False
        prev = self.sha
        self.sha = target
        record = DeployRecord(len(self.deploy_log) + 1, _now().isoformat(), target, prev, self.version,
                              "OCA Executor <oca@localhost>", f"restore {tag}", [], note)
        self.deploy_log.append(record)
        return record

    # ----------------------------------------------------------------- misc

    def describe(self) -> dict[str, Any]:
        return {"incident": self.incident, "effective_incident": self._effective_incident(), "config": self.config,
                "version": self.version, "sha": self.sha, "sandbox": self.sandbox, "branches": sorted(self.branches),
                "tags": self.tags, "failure_modes": sorted(self.failure_modes), "calls": self.calls[-20:]}
