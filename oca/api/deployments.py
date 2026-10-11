from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from oca.api.deps import get_db
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.schemas import Actor, Deployment, Role

router = APIRouter(prefix="/deployments", tags=["deployments"])


class RestoreIn(BaseModel):
    reason: str = "restore requested from dashboard"


@router.get("", response_model=list[Deployment])
async def list_deployments(limit: int = Query(100, ge=1, le=1000), db: Database = Depends(get_db),
                           _: Principal = Depends(current_principal)):
    return await db.list_deployments(limit)


@router.get("/{deployment_id}", response_model=Deployment)
async def get_deployment(deployment_id: str, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    deployment = await db.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(404, "Unknown deployment")
    return deployment


@router.post("/{deployment_id}/restore", response_model=Deployment)
async def restore(deployment_id: str, request: Request, body: RestoreIn | None = None, db: Database = Depends(get_db),
                  principal: Principal = Depends(require(Role.admin))):
    deployment = await db.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(404, "Unknown deployment")
    if not deployment.backup_ref:
        raise HTTPException(409, "This deployment has no backup to restore")
    reason = (body.reason if body else RestoreIn().reason) + f" (by {principal.name})"
    try:
        return await request.app.state.backup.restore(deployment_id, Actor.human, reason)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"restore failed: {exc}")
