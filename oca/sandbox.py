"""Sandbox proof loop: reproduce the incident, apply the Engineer's patch, prove the fix.

All measurements are taken by the server; the gate and Guardian trust ``sandbox_runs``,
never the Engineer's own claims.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI

from oca.fakeco import diffcheck
from oca.fakeco.runner import PatchResult, RunnerError
from oca.schemas import (
    Actor, EventType, RiskFlags, SandboxMetrics, SandboxRun, TestRunResult, TicketEventCreate,
)

log = logging.getLogger("oca.sandbox")


class SandboxError(RuntimeError):
    pass


class SandboxManager:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.db = app.state.db
        self.runner = app.state.runner
        self.settings = app.state.settings
        self._lock = asyncio.Lock()  # one sandbox at a time (driftwood has a single .sandbox worktree)
        self.active_ticket: str | None = None

    @staticmethod
    def branch_for(ticket_id: str) -> str:
        return f"fix/{ticket_id}"

    async def _thresholds(self) -> dict[str, float]:
        stored = await self.db.get_setting("thresholds", {}) or {}
        return {"error_rate_pct": float(stored.get("error_rate_pct", 5.0)),
                "p95_latency_ms": float(stored.get("p95_latency_ms", 1000.0))}

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
        out = []
        if metrics.error_rate_pct > thresholds["error_rate_pct"]:
            out.append("error_rate_pct")
        if metrics.p95_ms > thresholds["p95_latency_ms"]:
            out.append("p95_latency_ms")
        return out

    # ------------------------------------------------------- orchestrator steps

    async def prepare(self, ticket_id: str) -> SandboxRun:
        """Bring the sandbox up at the production sha and measure BEFORE. Must reproduce."""
        ticket = await self._ensure_ticket(ticket_id)
        async with self._lock:
            if self.active_ticket not in (None, ticket_id):
                await self.runner.sandbox_down()
            prod_sha = await self.runner.prod_sha()
            info = await self.runner.sandbox_up(None)
            self.active_ticket = ticket_id
            run = await self.db.create_sandbox_run(ticket_id, info.git_sha or prod_sha, await self._baseline())
            before = await self.runner.sandbox_loadtest(self.settings.loadtest_requests)
            thresholds = await self._thresholds()
            breaches = self._breaches(before, thresholds)
            reproduced = bool(breaches)
            run = await self.db.update_sandbox_run(run.id, before=before, reproduced=reproduced,
                                                   notes=f"sandbox at {info.git_sha} ({info.version})")
        await self._event(ticket_id, Actor.orchestrator, EventType.sandbox_result, {
            "phase": "before", "sandbox_run_id": run.id, "git_sha": run.base_sha, "reproduced": reproduced,
            "breaches": breaches, "before": before.model_dump(), "baseline": run.baseline,
            "detect": ticket.metrics_at_detect or {}})
        return run

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
            after = await self.runner.sandbox_loadtest(self.settings.loadtest_requests)
            tests = await self.runner.sandbox_tests()
            thresholds = await self._thresholds()
            breaches = self._breaches(after, thresholds)
            improved = run.before is None or after.error_rate_pct <= max(run.before.error_rate_pct / 4, thresholds["error_rate_pct"])
            passed = tests.passed and not breaches and improved
            run = await self.db.update_sandbox_run(run.id, after=after, tests=tests, passed=passed)
        await self._event(ticket_id, Actor.orchestrator, EventType.sandbox_result, {
            "phase": "after", "sandbox_run_id": run.id, "patch_ref": run.patch_ref, "passed": passed,
            "breaches": breaches, "tests": tests.model_dump(), "before": run.before.model_dump() if run.before else None,
            "after": after.model_dump(), "baseline": run.baseline})
        return run

    async def teardown(self, ticket_id: str) -> None:
        async with self._lock:
            if self.active_ticket in (None, ticket_id):
                try:
                    await self.runner.sandbox_down()
                except RunnerError as exc:
                    log.warning("sandbox down failed: %s", exc)
                self.active_ticket = None

    # ------------------------------------------------------------ engineer tools

    async def _run_for(self, ticket_id: str) -> SandboxRun:
        await self._ensure_ticket(ticket_id)
        run = await self.db.latest_sandbox_run(ticket_id)
        if run is None:
            raise SandboxError("The sandbox has not been prepared for this ticket (orchestrator runs prepare first)")
        return run

    async def apply_patch(self, ticket_id: str, diff: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        branch = self.branch_for(ticket_id)
        async with self._lock:
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            result: PatchResult = await self.runner.apply_patch_in_sandbox(
                branch, diff, f"{ticket_id}: Engineer patch {len(diff.splitlines())} lines")
            if result.applied:
                await self.runner.sandbox_up(branch)  # rebuild the sandbox containers from the fix branch
                run = await self.db.update_sandbox_run(run.id, patch_ref=branch, after=None, tests=None, passed=None)
        flags = diffcheck.risk_flags(diff) if result.applied else RiskFlags()
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": "apply_patch", "applied": result.applied, "branch": result.branch, "commit": result.commit_sha,
            "files_changed": result.files_changed, "error": result.error, "risk_flags": flags.model_dump()})
        return {"evidence_ref": f"sandbox:{run.id}:patch:{result.commit_sha or 'rejected'}", **result.as_dict(),
                "patch_ref": branch if result.applied else None, "risk_flags": flags.model_dump(),
                "sandbox_run_id": run.id}

    async def run_tests(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        async with self._lock:
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            tests: TestRunResult = await self.runner.sandbox_tests()
            run = await self.db.update_sandbox_run(run.id, tests=tests)
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": "run_tests", "passed": tests.passed, "failed": tests.failed, "total": tests.total, "ref": run.patch_ref or run.base_sha})
        return {"evidence_ref": f"sandbox:{run.id}:tests", "sandbox_run_id": run.id, "ref": run.patch_ref or run.base_sha,
                **tests.model_dump()}

    async def run_loadtest(self, ticket_id: str, requests: int | None) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        n = requests or self.settings.loadtest_requests
        async with self._lock:
            if self.active_ticket != ticket_id:
                await self._ensure_up(run)
            metrics = await self.runner.sandbox_loadtest(n)
            thresholds = await self._thresholds()
            breaches = self._breaches(metrics, thresholds)
            phase = "after" if run.patch_ref else "before"
            if phase == "before":
                run = await self.db.update_sandbox_run(run.id, before=metrics, reproduced=bool(breaches))
            else:
                run = await self.db.update_sandbox_run(run.id, after=metrics)
        await self._event(ticket_id, Actor.engineer, EventType.finding, {
            "tool": "run_loadtest", "phase": phase, "requests": n, "error_rate_pct": metrics.error_rate_pct,
            "p95_ms": metrics.p95_ms, "breaches": breaches})
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
                "diff": text[:100_000], "files_changed": info.files, "lines_added": info.added, "lines_removed": info.removed,
                "risk_flags": diffcheck.risk_flags(text).model_dump()}

    async def test_results(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        return {"evidence_ref": f"sandbox:{run.id}:tests", "sandbox_run_id": run.id, "patch_ref": run.patch_ref,
                "tests": run.tests.model_dump() if run.tests else None}

    async def results(self, ticket_id: str) -> dict[str, Any]:
        run = await self._run_for(ticket_id)
        return {"evidence_ref": f"sandbox:{run.id}:results", **run.model_dump(mode="json")}
