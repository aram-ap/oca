"""Sandbox proof loop: reproduce the incident, apply the Engineer's patch, prove the fix.

All measurements are taken by the server; the gate and Guardian trust ``sandbox_runs``,
never the Engineer's own claims.
"""

from __future__ import annotations

import asyncio
import math
import logging
import re
from typing import Any

from fastapi import FastAPI

from oca.fakeco import diffcheck
from oca.fakeco.runner import PatchResult, RunnerError
from oca.schemas import (
    Actor, EventType, RiskFlags, SandboxMetrics, SandboxRun, TestRunResult, TicketEventCreate,
)

log = logging.getLogger("oca.sandbox")


RECORDED_DIFF_CHARS = 4000
DEFAULT_LOAD_RATE = 30     # requests/s when production's rate is unknown
MAX_LOAD_RATE = 200
LOAD_MARGIN = 1.5          # sandbox rate = production rate at detection x margin
MIN_LOADTEST_S = 40        # long enough for the sandbox Prometheus to record pool utilisation
RECORDED_OUTPUT_CHARS = 1500
MAX_RECORDED_COMMANDS = 40
APP_ENV = "services/orders-api/config/app.env"
MIGRATIONS_DIR = "services/orders-api/migrations"


class SandboxError(RuntimeError):
    pass


class SandboxManager:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.db = app.state.db
        self.runner = app.state.runner
        self.settings = app.state.settings
        self._lock = asyncio.Lock()  # one OCA-managed environment at a time
        self.active_ticket: str | None = None

    @staticmethod
    def branch_for(ticket_id: str) -> str:
        return f"fix/{ticket_id}"

    async def _thresholds(self) -> dict[str, float]:
        stored = await self.db.get_setting("thresholds", {}) or {}
        return {"error_rate_pct": float(stored.get("error_rate_pct", 5.0)),
                "p95_latency_ms": float(stored.get("p95_latency_ms", 1000.0)),
                "db_pool_utilization_pct": float(stored.get("db_pool_utilization_pct", 90.0))}

    async def _load(self, ticket_id: str, requests: int | None = None) -> tuple[int, int]:
        """(requests, rate) for a sandbox load test: production's request rate at detection with a margin.

        A fixed 30 rps never reproduces a load-driven incident. The run lasts at least
        MIN_LOADTEST_S so the sandbox Prometheus sees pool utilisation during the test.
        """
        ticket = await self.db.get_ticket(ticket_id)
        detect_rps = float(((ticket.metrics_at_detect if ticket else None) or {}).get("rps") or 0.0)
        rate = int(min(MAX_LOAD_RATE, max(DEFAULT_LOAD_RATE, math.ceil(detect_rps * LOAD_MARGIN))))
        n = int(requests or self.settings.loadtest_requests)
        n = min(10_000, max(n, rate * MIN_LOADTEST_S))
        return n, rate

    async def _production(self) -> dict[str, Any]:
        """The live production SLO state, attached to prepare() so the orchestrator can tell
        'not reproduced but production is still down' from a transient blip."""
        try:
            snap = await self.runner.status()
        except Exception as exc:  # noqa: BLE001 - informational
            return {"healthy": None, "breaches": [], "error": str(exc)[:200]}
        return {"healthy": snap.healthy, "breaches": list(snap.breaches), "metrics": snap.metrics()}

    async def _baseline(self) -> dict[str, float]:
        stored = await self.db.get_setting("baseline", {}) or {}
        return {k: float(v) for k, v in stored.items() if isinstance(v, (int, float))}

    async def _event(self, ticket_id: str, actor: Actor, type: EventType, payload: dict[str, Any]) -> None:
        await self.db.append_event(ticket_id, TicketEventCreate(actor=actor, type=type, payload=payload))

    async def _ensure_ticket(self, ticket_id: str):
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            raise SandboxError(f"Unknown ticket {ticket_id}")
        return ticket

    async def _ensure_up(self, run: SandboxRun) -> None:
        ref = run.patch_ref or run.base_sha
        await self.runner.sandbox_up(ref)
        self.active_ticket = run.ticket_id

    def _breaches(self, metrics: SandboxMetrics, thresholds: dict[str, float]) -> list[str]:
        """The production detector's SLO set, applied to what the sandbox measured."""
        out = []
        if metrics.error_rate_pct > thresholds["error_rate_pct"]:
            out.append("error_rate_pct")
        if metrics.p95_ms > thresholds["p95_latency_ms"]:
            out.append("p95_latency_ms")
        if metrics.db_pool_utilization_pct is not None and metrics.db_pool_utilization_pct > thresholds["db_pool_utilization_pct"]:
            out.append("db_pool_utilization_pct")
        return out

    # ------------------------------------------------------- orchestrator steps

    async def prepare(self, ticket_id: str) -> SandboxRun:
        """Bring the sandbox up at the production sha and measure BEFORE. Must reproduce."""
        ticket = await self._ensure_ticket(ticket_id)
        async with self._lock:
            # Each reproduction starts with fresh seeded data. A previous patch
            # may have applied a migration which a code checkout cannot undo.
            # Also clears a sandbox left by a previous OCA process.
            await self.runner.sandbox_down()
            self.active_ticket = None
            try:
                prod_sha = await self.runner.prod_sha()
                self.active_ticket = ticket_id
                info = await self.runner.sandbox_up(prod_sha)
                run = await self.db.create_sandbox_run(ticket_id, info.git_sha or prod_sha, await self._baseline())
                n, rate = await self._load(ticket_id)
                before = await self.runner.sandbox_loadtest(n, rate)
                thresholds = await self._thresholds()
                breaches = self._breaches(before, thresholds)
                reproduced = bool(breaches)
                run = await self.db.update_sandbox_run(run.id, before=before, reproduced=reproduced,
                                                       notes=f"sandbox at {info.git_sha} ({info.version})")
            except (Exception, asyncio.CancelledError):
                try:
                    await asyncio.shield(self.runner.sandbox_down())
                    self.active_ticket = None
                except Exception:
                    log.exception("sandbox cleanup after failed preparation needs retry")
                raise
        production = await self._production()
        await self._event(ticket_id, Actor.orchestrator, EventType.sandbox_result, {
            "phase": "before", "sandbox_run_id": run.id, "git_sha": run.base_sha, "reproduced": reproduced,
            "breaches": breaches, "before": before.model_dump(), "baseline": run.baseline,
            "detect": ticket.metrics_at_detect or {}, "load": {"requests": n, "rate": rate}, "production": production})
        return run.model_copy(update={"production": production})

    async def validate(self, ticket_id: str) -> SandboxRun:
        """Rebuild on the fix branch, measure AFTER, run tests, compare with baseline."""
        await self._ensure_ticket(ticket_id)
        run = await self.db.latest_sandbox_run(ticket_id)
        if run is None or run.reproduced is None:
            raise SandboxError("prepare() has not run for this ticket")
        if run.patch_ref is None:
            raise SandboxError("no patch has been applied in the sandbox")
        async with self._lock:
            await self._ensure_up(run)
            n, rate = await self._load(ticket_id)
            after = await self.runner.sandbox_loadtest(n, rate)
            tests = await self.runner.sandbox_tests()
            thresholds = await self._thresholds()
            breaches = self._breaches(after, thresholds)
            improved = run.before is None or after.error_rate_pct <= max(run.before.error_rate_pct / 4, thresholds["error_rate_pct"])
            passed = tests.passed and not breaches and improved  # `not breaches` covers pool and latency too
            run = await self.db.update_sandbox_run(run.id, after=after, tests=tests, passed=passed)
        await self._event(ticket_id, Actor.orchestrator, EventType.sandbox_result, {
            "phase": "after", "sandbox_run_id": run.id, "patch_ref": run.patch_ref, "passed": passed,
            "breaches": breaches, "tests": tests.model_dump(), "before": run.before.model_dump() if run.before else None,
            "after": after.model_dump(), "baseline": run.baseline, "load": {"requests": n, "rate": rate}})
        return run

    async def teardown(self, ticket_id: str) -> None:
        async with self._lock:
            if self.active_ticket in (None, ticket_id):
                try:
                    await self.runner.sandbox_down()
                except RunnerError as exc:
                    log.warning("sandbox down failed: %s", exc)
                    raise
                self.active_ticket = None

    # ------------------------------------------------------------ engineer tools

    def _mark(self) -> int:
        mark = getattr(self.runner, "command_mark", None)
        return mark() if mark else 0

    def _commands(self, mark: int) -> list[dict[str, Any]]:
        """Shell commands the runner executed since ``mark`` (shown on the ticket timeline)."""
        since = getattr(self.runner, "commands_since", None)
        commands = since(mark) if since else []
        return commands[-MAX_RECORDED_COMMANDS:]

    async def _run_for(self, ticket_id: str) -> SandboxRun:
        await self._ensure_ticket(ticket_id)
        run = await self.db.latest_sandbox_run(ticket_id)
        if run is None:
            raise SandboxError("The sandbox has not been prepared for this ticket (orchestrator runs prepare first)")
        return run

    async def apply_patch(self, ticket_id: str, diff: str, tool: str = "apply_patch",
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        branch = self.branch_for(ticket_id)
        async with self._lock:
            mark = self._mark()
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            result: PatchResult = await self.runner.apply_patch_in_sandbox(
                branch, diff, f"{ticket_id}: Engineer patch {len(diff.splitlines())} lines")
            if result.applied:
                await self.runner.sandbox_up(branch)  # rebuild the sandbox containers from the fix branch
                run = await self.db.update_sandbox_run(run.id, patch_ref=branch, after=None, tests=None, passed=None)
            commands = self._commands(mark)
        flags = diffcheck.risk_flags(diff) if result.applied else RiskFlags()
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": tool, "applied": result.applied, "branch": result.branch, "commit": result.commit_sha,
            "files_changed": result.files_changed, "stat": result.stat, "error": result.error, "risk_flags": flags.model_dump(),
            "diff": diff[:RECORDED_DIFF_CHARS], "diff_truncated": len(diff) > RECORDED_DIFF_CHARS, "commands": commands,
            **(extra or {})})
        out = {"evidence_ref": f"sandbox:{run.id}:patch:{result.commit_sha or 'rejected'}", **result.as_dict(),
               "patch_ref": branch if result.applied else None, "risk_flags": flags.model_dump(), "sandbox_run_id": run.id,
               "diff": diff}
        if not result.applied:
            out["hint"] = diffcheck.FORMAT_HINT
        return out

    async def set_config(self, ticket_id: str, key: str, value: int) -> dict[str, Any]:
        """Engineer tool: the server writes the diff for one KEY=value change in app.env, then applies it."""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
            raise SandboxError(f"Invalid config key {key!r}: use UPPER_SNAKE_CASE")
        await self._run_for(ticket_id)
        try:
            content = await self.runner.read_sandbox_file(APP_ENV)
        except FileNotFoundError:
            raise SandboxError(f"{APP_ENV} does not exist in the sandbox")
        try:
            diff = diffcheck.config_diff(APP_ENV, content, key, int(value))
        except ValueError as exc:
            raise SandboxError(str(exc))
        out = await self.apply_patch(ticket_id, diff, tool="set_config", extra={"key": key, "value": int(value), "path": APP_ENV})
        return {**out, "key": key, "value": int(value), "path": APP_ENV}

    async def create_migration(self, ticket_id: str, name: str, sql: str) -> dict[str, Any]:
        """Engineer tool: the server numbers and writes a new migration file, then applies it as a patch."""
        name = name.strip().removesuffix(".sql")
        name = re.sub(r"^\d+_", "", name)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", name):
            raise SandboxError(f"Invalid migration name {name!r}: use lower_snake_case without a number")
        if not sql.strip():
            raise SandboxError("Migration SQL is empty")
        await self._run_for(ticket_id)
        try:
            existing = await self.runner.list_files(MIGRATIONS_DIR)
        except FileNotFoundError:
            existing = []
        numbers = [int(m.group(1)) for f in existing if (m := re.match(r"(\d+)_", f.rsplit("/", 1)[-1]))]
        path = f"{MIGRATIONS_DIR}/{max(numbers, default=0) + 1:03d}_{name}.sql"
        if any(f.rsplit("/", 1)[-1].endswith(f"_{name}.sql") for f in existing):
            raise SandboxError(f"A migration named {name} already exists: {existing}")
        diff = diffcheck.new_file_diff(path, sql.strip() + "\n")
        out = await self.apply_patch(ticket_id, diff, tool="create_migration", extra={"path": path})
        return {**out, "path": path}

    async def run_tests(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        async with self._lock:
            mark = self._mark()
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            tests: TestRunResult = await self.runner.sandbox_tests()
            run = await self.db.update_sandbox_run(run.id, tests=tests)
            commands = self._commands(mark)
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": "run_tests", "passed": tests.passed, "failed": tests.failed, "total": tests.total, "ref": run.patch_ref or run.base_sha,
            "command": tests.command, "output": tests.output_tail[-RECORDED_OUTPUT_CHARS:], "commands": commands})
        return {"evidence_ref": f"sandbox:{run.id}:tests", "sandbox_run_id": run.id, "ref": run.patch_ref or run.base_sha,
                **tests.model_dump()}

    async def run_loadtest(self, ticket_id: str, requests: int | None) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        n, rate = await self._load(ticket_id, requests)
        async with self._lock:
            mark = self._mark()
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            metrics = await self.runner.sandbox_loadtest(n, rate)
            thresholds = await self._thresholds()
            breaches = self._breaches(metrics, thresholds)
            phase = "after" if run.patch_ref else "before"
            if phase == "before":
                run = await self.db.update_sandbox_run(run.id, before=metrics, reproduced=bool(breaches))
            else:
                run = await self.db.update_sandbox_run(run.id, after=metrics)
            commands = self._commands(mark)
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": "run_loadtest", "phase": phase, "requests": n, "rate": rate, "error_rate_pct": metrics.error_rate_pct,
            "p95_ms": metrics.p95_ms, "db_pool_utilization_pct": metrics.db_pool_utilization_pct, "breaches": breaches,
            "commands": commands})
        return {"evidence_ref": f"sandbox:{run.id}:loadtest:{phase}", "sandbox_run_id": run.id, "phase": phase,
                "ref": run.patch_ref or run.base_sha, "metrics": metrics.model_dump(), "breaches": breaches,
                "baseline": run.baseline, "thresholds": thresholds, "incident_present": bool(breaches)}

    # ------------------------------------------------------------ guardian tools

    async def diff(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        if run.patch_ref is None:
            return {"evidence_ref": f"sandbox:{run.id}:diff", "sandbox_run_id": run.id, "patch_ref": None, "diff": "",
                    "files_changed": [], "risk_flags": RiskFlags().model_dump(), "note": "no patch applied yet"}
        text = await self.runner.sandbox_diff(run.patch_ref)
        info = diffcheck.parse(text)
        return {"evidence_ref": f"sandbox:{run.id}:diff", "sandbox_run_id": run.id, "patch_ref": run.patch_ref,
                "diff": text[:14_000], "truncated": len(text) > 14_000, "files_changed": info.files,
                "lines_added": info.added, "lines_removed": info.removed,
                "risk_flags": diffcheck.risk_flags(text).model_dump()}

    async def test_results(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        return {"evidence_ref": f"sandbox:{run.id}:tests", "sandbox_run_id": run.id, "patch_ref": run.patch_ref,
                "tests": run.tests.model_dump() if run.tests else None}

    async def results(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        return {"evidence_ref": f"sandbox:{run.id}:results", **run.model_dump(mode="json")}
