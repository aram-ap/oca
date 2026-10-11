"""Run executor: performs an approved action against production, staged and monitored."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI

from oca.checks import PRIMARY_CHECK
from oca.checks.status_check import StatusCheck
from oca.schemas import (
    ActionName, ActionStatus, Actor, CheckResult, Deployment, DeploymentStatus, EventType,
    TicketEventCreate, TicketStatus, utcnow,
)

log = logging.getLogger("oca.executor")
STAGES = [("canary", 10), ("monitor", 10), ("widen", 50), ("monitor", 50), ("full", 100)]
SLO_KEYS = ("error_rate_pct", "p95_latency_ms", "db_pool_utilization_pct")
REGRESSION_TOLERANCE = 0.10  # a breaching metric more than 10% above its pre-deploy value is a regression
IMPROVEMENT_MIN = 0.20       # at least one SLO metric 20% better than pre-deploy counts as improved


class Executor:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.db = app.state.db
        self.runner = app.state.runner
        self.settings = app.state.settings
        self.tasks: dict[str, asyncio.Task] = {}

    def launch(self, deployment_id: str) -> asyncio.Task:
        task = asyncio.create_task(self.run(deployment_id), name=f"oca-executor-{deployment_id}")
        self.tasks[deployment_id] = task
        return task

    async def wait(self, deployment_id: str) -> None:
        task = self.tasks.get(deployment_id)
        if task is not None:
            await task

    # ------------------------------------------------------------------ helpers

    async def _event(self, ticket_id: str | None, type: EventType, payload: dict[str, Any], actor: Actor = Actor.executor) -> None:
        if ticket_id:
            await self.db.append_event(ticket_id, TicketEventCreate(actor=actor, type=type, payload=payload))

    async def _stage(self, dep: Deployment, stage: str, pct: int, **extra: Any) -> None:
        await self._event(dep.ticket_id, EventType.run, {"stage": stage, "pct": pct, "deployment_id": dep.id, **extra})

    async def _check(self) -> CheckResult:
        scheduler = getattr(self.app.state, "scheduler", None)
        if scheduler is not None and PRIMARY_CHECK in scheduler.checks:
            return await scheduler.run_check(PRIMARY_CHECK, handle=False)
        return await StatusCheck(self.runner).run()

    async def _perform(self, dep: Deployment) -> dict[str, Any]:
        args = dep.args
        note = dep.ticket_id or dep.id
        if dep.action is ActionName.deploy_patch:
            record = await self.runner.deploy_branch(args["branch"], note)
        elif dep.action is ActionName.set_config:
            record = await self.runner.set_config_and_deploy(args["key"], int(args["value"]), note)
        elif dep.action is ActionName.restart_service:
            await self.runner.restart_service(args["service"])
            return {"restarted": args["service"]}
        elif dep.action is ActionName.rollback:
            target = await self.db.get_deployment(args["deployment_id"])
            if target is None or not target.backup_ref:
                raise RuntimeError(f"{args['deployment_id']} has no backup to restore")
            record = await self.runner.restore(target.backup_ref, f"rollback requested ({note})")
            await self.db.update_deployment(target.id, status=DeploymentStatus.rolled_back)
        else:  # pragma: no cover - the gate already rejected unknown actions
            raise RuntimeError(f"unsupported action {dep.action}")
        await self.db.update_deployment(dep.id, git_sha=record.git_sha, driftwood_deploy_id=record.deploy_id)
        return record.as_dict()

    # --------------------------------------------------------------------- run

    async def run(self, deployment_id: str) -> Deployment:
        dep = await self.db.get_deployment(deployment_id)
        if dep is None:
            raise KeyError(deployment_id)
        ticket = await self.db.get_ticket(dep.ticket_id) if dep.ticket_id else None
        backup = self.app.state.backup
        try:
            await backup.create(dep)
            dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.running)
            if ticket:
                await self.db.set_status(ticket.id, TicketStatus.deploying, Actor.executor, deployment_id=dep.id)
            performed: dict[str, Any] = {}
            pre = await self._check()  # what production looked like just before the change: the regression baseline
            dep = await self.db.update_deployment(dep.id, evidence={**dep.evidence, "pre_deploy": pre.metrics})
            for stage, pct in STAGES:
                if stage == "canary":
                    performed = await self._perform(dep)
                    await self._stage(dep, stage, pct, result=performed)
                elif stage == "monitor":
                    await asyncio.sleep(self.settings.stage_delay_s)
                    snap = await self._check()
                    await self._stage(dep, stage, pct, ok=snap.ok, metrics=snap.metrics, detail=snap.detail)
                else:
                    await self._stage(dep, stage, pct)

            dep = await self._conclude(dep, ticket, pre.metrics)
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the ticket
            dep = await self._fail(dep, ticket, exc)
        return dep

    async def _conclude(self, dep: Deployment, ticket, pre: dict[str, float]) -> Deployment:
        """Verify production after the change is fully applied and settle the deployment and ticket."""
        verdict = await self._verify(dep, ticket, pre)
        if verdict["ok"]:
            dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.succeeded, finished_at=utcnow(),
                                                  evidence={**dep.evidence, "verify": verdict["runs"]})
            if ticket:
                await self._resolve(ticket.id, dep)
        elif verdict["worse"] or not verdict["improved"] or ticket is None:
            dep = await self._auto_restore(dep, ticket, verdict)
        else:
            dep = await self._hold(dep, ticket, verdict)
        return dep

    async def _fail(self, dep: Deployment, ticket, exc: Exception) -> Deployment:
        log.exception("deployment %s failed", dep.id)
        dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.failed, finished_at=utcnow(),
                                              evidence={**dep.evidence, "error": str(exc)})
        await self._event(dep.ticket_id, EventType.error, {"where": "executor", "deployment_id": dep.id, "error": str(exc)})
        if ticket:
            await self.db.set_status(ticket.id, TicketStatus.failed, Actor.executor, note=str(exc)[:200])
        return dep

    # ------------------------------------------------------------------ resume

    async def _last_stage(self, dep: Deployment) -> str | None:
        if not dep.ticket_id:
            return None
        stages = [e.payload.get("stage") for e in await self.db.list_events(dep.ticket_id)
                  if e.type is EventType.run and e.payload.get("deployment_id") == dep.id]
        return stages[-1] if stages else None

    async def resume_interrupted(self) -> list[str]:
        """After a server restart, pick up deployments a previous process left ``running``.

        Once the change reached the ``full`` stage it is live in production, so the only thing missing is
        verification: rerun that and conclude as usual. If the process died before that (backup, canary,
        widen) the state of production is ambiguous, so the deployment and ticket are marked failed for a
        human to inspect rather than guessed at."""
        resumed: list[str] = []
        for dep in await self.db.list_deployments(limit=200):
            if dep.status is not DeploymentStatus.running or dep.id in self.tasks:
                continue
            stage = await self._last_stage(dep)
            ticket = await self.db.get_ticket(dep.ticket_id) if dep.ticket_id else None
            if stage in ("full", "settle", "verify"):
                log.warning("resuming verification of %s (last stage %r before the restart)", dep.id, stage)
                await self._stage(dep, "resume", 100, reason="server restarted during verification; re-verifying")
                self.tasks[dep.id] = asyncio.create_task(self._resume_verify(dep, ticket), name=f"oca-executor-{dep.id}")
            else:
                reason = f"server restarted during the {stage or 'backup'} stage; production state unverified"
                log.warning("%s: %s", dep.id, reason)
                await self._fail(dep, ticket, RuntimeError(reason))
            resumed.append(dep.id)
        return resumed

    async def _resume_verify(self, dep: Deployment, ticket) -> Deployment:
        try:
            return await self._conclude(dep, ticket, dict(dep.evidence.get("pre_deploy") or {}))
        except Exception as exc:  # noqa: BLE001
            return await self._fail(dep, ticket, exc)

    async def _verify(self, dep: Deployment, ticket, pre: dict[str, float] | None = None) -> dict[str, Any]:
        """Three outcomes: ok (healthy N times in a row), worse (regression vs. just before the deploy), or improved-but-breaching.

        Under external load a correct fix may not bring production inside its SLOs; rolling it back
        for that alone would undo an improvement. A regression is still reverted automatically.
        """
        runs = []
        pre = pre or {}
        detect_error = float((ticket.metrics_at_detect or {}).get("error_rate_pct", 0.0)) if ticket else 0.0
        worse = False
        if self.settings.verify_settle_s > 0:
            # The detector's series are 1-minute rates: read too early they still contain pre-deploy samples.
            await self._stage(dep, "settle", 100, seconds=self.settings.verify_settle_s)
            await asyncio.sleep(self.settings.verify_settle_s)
        needed, streak, max_runs = self.settings.verify_runs, 0, self.settings.verify_runs * 2
        for i in range(max_runs):
            if i:
                await asyncio.sleep(self.settings.verify_interval_s)
            result = await self._check()
            streak = streak + 1 if result.ok else 0
            runs.append({"run": i + 1, "ok": result.ok, "metrics": result.metrics, "detail": result.detail,
                         "breaches": result.breaches})
            await self._stage(dep, "verify", 100, run=i + 1, of=needed, ok=result.ok,
                              metrics=result.metrics, detail=result.detail)
            if not result.ok and ((detect_error and result.metrics.get("error_rate_pct", 0.0) > detect_error)
                                  or self._regressed(result, pre)):
                worse = True
                break
            if streak >= needed:
                break  # N consecutive healthy reads; a single stale read earlier does not count against the fix
        ok = streak >= needed
        improved = (not ok and not worse and bool(pre) and bool(runs) and self._improved(runs[-1]["metrics"], pre))
        return {"ok": ok, "worse": worse, "improved": improved, "runs": runs, "pre_deploy": pre}

    @staticmethod
    def _regressed(result: CheckResult, pre: dict[str, float]) -> bool:
        """A breaching SLO metric that is clearly higher than just before the deploy, or a breach that is new."""
        for key in SLO_KEYS:
            now, before = result.metrics.get(key), pre.get(key)
            if key not in result.breaches or now is None:
                continue
            if before is None or now > before * (1 + REGRESSION_TOLERANCE):
                return True
        return False

    @staticmethod
    def _improved(now: dict[str, float], pre: dict[str, float]) -> bool:
        """Every SLO metric is no worse than before the deploy and at least one is materially better."""
        better, worse = False, False
        for key in SLO_KEYS:
            a, b = now.get(key), pre.get(key)
            if a is None or b is None:
                continue
            if a > b * (1 + REGRESSION_TOLERANCE):
                worse = True
            if a < b * (1 - IMPROVEMENT_MIN):
                better = True
        return better and not worse

    async def _hold(self, dep: Deployment, ticket, verdict: dict[str, Any]) -> Deployment:
        """Keep the deployment, but do not claim victory: a human confirms or rolls back from the dashboard."""
        last = verdict["runs"][-1]
        reason = (f"production improved but still breaches {', '.join(last.get('breaches') or []) or 'its SLOs'} "
                  f"after the change; held for human verification (external load?)")
        await self._stage(dep, "hold", 100, reason=reason, metrics=last["metrics"], pre_deploy=verdict["pre_deploy"],
                          runs=verdict["runs"])
        dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.succeeded, finished_at=utcnow(),
                                              evidence={**dep.evidence, "verify": verdict["runs"], "held": True})
        await self.db.set_status(ticket.id, TicketStatus.awaiting_verification, Actor.executor, note=reason, deployment_id=dep.id)
        return dep

    async def confirm(self, ticket_id: str, decided_by: str, note: str = "") -> Deployment:
        """Operator confirms a held deployment: the incident is resolved as deployed."""
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None or ticket.status is not TicketStatus.awaiting_verification or not ticket.current_deployment_id:
            raise ValueError(f"{ticket_id} is not awaiting verification")
        dep = await self.db.get_deployment(ticket.current_deployment_id)
        await self._event(ticket_id, EventType.human_decision, {"decision": "confirm", "note": note, "by": decided_by,
                                                                "deployment_id": dep.id}, actor=Actor.human)
        await self._resolve(ticket_id, dep)
        return dep

    async def reject(self, ticket_id: str, decided_by: str, note: str = "") -> Deployment:
        """Operator rolls a held deployment back from the dashboard."""
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None or ticket.status is not TicketStatus.awaiting_verification or not ticket.current_deployment_id:
            raise ValueError(f"{ticket_id} is not awaiting verification")
        reason = f"rolled back by {decided_by} after verification hold" + (f": {note}" if note else "")
        await self._event(ticket_id, EventType.human_decision, {"decision": "roll back", "note": note, "by": decided_by,
                                                                "deployment_id": ticket.current_deployment_id}, actor=Actor.human)
        try:
            await self.app.state.backup.restore(ticket.current_deployment_id, Actor.human, reason, ticket_id=ticket_id)
        finally:
            await self.db.set_status(ticket_id, TicketStatus.rolled_back, Actor.human, note=reason)
        return await self.db.get_deployment(ticket.current_deployment_id)

    async def _resolve(self, ticket_id: str, dep: Deployment) -> None:
        await self.db.set_status(ticket_id, TicketStatus.resolved, Actor.executor, deployment_id=dep.id)
        await self.db.update_ticket(ticket_id, pending_action_id=None)
        mttr = await self.db.mttr(ticket_id)
        await self._event(ticket_id, EventType.resolved, {"deployment_id": dep.id, "mttr": mttr.model_dump(),
                                                          "git_sha": dep.git_sha})
        sandbox = getattr(self.app.state, "sandbox", None)
        if sandbox is not None:
            try:
                await sandbox.teardown(ticket_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("sandbox teardown after %s failed: %s", ticket_id, exc)

    async def _auto_restore(self, dep: Deployment, ticket, verdict: dict[str, Any]) -> Deployment:
        reason = ("post-deploy health worse than before the change" if verdict["worse"]
                  else "post-deploy health checks still failing with no measurable improvement")
        await self._event(dep.ticket_id, EventType.run, {"stage": "auto_restore", "pct": 100, "deployment_id": dep.id,
                                                         "reason": reason, "runs": verdict["runs"]})
        try:
            await self.app.state.backup.restore(dep.id, Actor.executor, reason, ticket_id=dep.ticket_id)
        finally:
            dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.rolled_back, finished_at=utcnow(),
                                                  evidence={**dep.evidence, "verify": verdict["runs"], "restored": True})
            if ticket:
                await self.db.set_status(ticket.id, TicketStatus.rolled_back, Actor.executor, note=reason)
        return dep


async def start_executor_resume(app: FastAPI) -> None:
    """Service starter: finish deployments the previous process was verifying when it stopped."""
    await app.state.executor.resume_interrupted()
    return None
