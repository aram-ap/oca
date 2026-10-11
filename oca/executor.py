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

            verdict = await self._verify(dep, ticket)
            if verdict["ok"]:
                dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.succeeded, finished_at=utcnow(),
                                                      evidence={**dep.evidence, "verify": verdict["runs"]})
                if ticket:
                    await self._resolve(ticket.id, dep)
            else:
                dep = await self._auto_restore(dep, ticket, verdict)
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the ticket
            log.exception("deployment %s failed", dep.id)
            dep = await self.db.update_deployment(dep.id, status=DeploymentStatus.failed, finished_at=utcnow(),
                                                  evidence={**dep.evidence, "error": str(exc)})
            await self._event(dep.ticket_id, EventType.error, {"where": "executor", "deployment_id": dep.id, "error": str(exc)})
            if ticket:
                await self.db.set_status(ticket.id, TicketStatus.failed, Actor.executor, note=str(exc)[:200])
        return dep

    async def _verify(self, dep: Deployment, ticket) -> dict[str, Any]:
        runs = []
        detect_error = float((ticket.metrics_at_detect or {}).get("error_rate_pct", 0.0)) if ticket else 0.0
        worse = False
        for i in range(self.settings.verify_runs):
            if i:
                await asyncio.sleep(self.settings.verify_interval_s)
            result = await self._check()
            runs.append({"run": i + 1, "ok": result.ok, "metrics": result.metrics, "detail": result.detail})
            await self._stage(dep, "verify", 100, run=i + 1, of=self.settings.verify_runs, ok=result.ok,
                              metrics=result.metrics, detail=result.detail)
            if not result.ok and detect_error and result.metrics.get("error_rate_pct", 0.0) > detect_error:
                worse = True
                break
        ok = bool(runs) and all(r["ok"] for r in runs)
        return {"ok": ok, "worse": worse, "runs": runs}

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
        reason = "post-deploy health worse than at detection" if verdict["worse"] else "post-deploy health checks still failing"
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
