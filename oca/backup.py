"""Backups before every change; restore from the dashboard or automatically."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI

from oca.fakeco.runner import BackupRef
from oca.schemas import (
    ActionName, Actor, Deployment, DeploymentStatus, EventType, TicketEventCreate, TicketStatus, utcnow,
)

log = logging.getLogger("oca.backup")


class BackupManager:
    def __init__(self, app: FastAPI) -> None:
        self.db = app.state.db
        self.runner = app.state.runner
        self.dir: Path = Path(app.state.settings.data_dir) / "backups"

    @staticmethod
    def tag_for(deployment_id: str) -> str:
        return f"backup/{deployment_id}"

    async def create(self, deployment: Deployment) -> BackupRef:
        ref = await self.runner.backup(self.tag_for(deployment.id))
        folder = self.dir / deployment.id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            records = await self.runner.deployments(limit=1)
            (folder / "deploy_record.json").write_text(json.dumps(records[0].as_dict() if records else {}, indent=2))
            try:
                (folder / "app.env").write_text(await self.runner.read_file("services/orders-api/config/app.env"))
            except (FileNotFoundError, PermissionError):
                pass
            (folder / "backup.json").write_text(json.dumps({"tag": ref.tag, "sha": ref.sha, "taken_at": utcnow().isoformat()}, indent=2))
            ref.path = str(folder)
        except OSError as exc:
            log.warning("could not write backup snapshot files: %s", exc)
        await self.db.update_deployment(deployment.id, backup_ref=ref.tag, prev_sha=ref.sha)
        if deployment.ticket_id:
            await self.db.append_event(deployment.ticket_id, TicketEventCreate(
                actor=Actor.executor, type=EventType.run,
                payload={"stage": "snapshot", "pct": 0, "deployment_id": deployment.id, "backup_ref": ref.tag,
                         "sha": ref.sha, "path": ref.path}))
        return ref

    async def restore(self, deployment_id: str, actor: Actor, reason: str, ticket_id: str | None = None) -> Deployment:
        original = await self.db.get_deployment(deployment_id)
        if original is None:
            raise KeyError(deployment_id)
        if not original.backup_ref:
            raise ValueError(f"{deployment_id} has no backup to restore")
        ticket_id = ticket_id or original.ticket_id
        rollback = await self.db.create_deployment(ticket_id, ActionName.rollback, {"deployment_id": deployment_id},
                                                   evidence={"reason": reason, "restores": original.backup_ref},
                                                   note=reason, status=DeploymentStatus.running)
        try:
            record = await self.runner.restore(original.backup_ref, f"restore {deployment_id}: {reason}")
        except Exception as exc:
            await self.db.update_deployment(rollback.id, status=DeploymentStatus.failed, finished_at=utcnow(),
                                            evidence={"reason": reason, "error": str(exc)})
            raise
        rollback = await self.db.update_deployment(rollback.id, status=DeploymentStatus.succeeded, finished_at=utcnow(),
                                                   git_sha=record.git_sha, prev_sha=record.prev_sha,
                                                   driftwood_deploy_id=record.deploy_id)
        await self.db.update_deployment(deployment_id, status=DeploymentStatus.rolled_back)
        if ticket_id:
            await self.db.append_event(ticket_id, TicketEventCreate(
                actor=actor, type=EventType.rolled_back,
                payload={"deployment_id": deployment_id, "rollback_id": rollback.id, "backup_ref": original.backup_ref,
                         "reason": reason, "restored_sha": record.git_sha}))
        return rollback
