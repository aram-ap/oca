from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from oca.api.deps import get_db, get_scheduler
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.schemas import CheckResult, HealthCheckInfo, Role

router = APIRouter(prefix="/checks", tags=["checks"])


class CheckUpdate(BaseModel):
    enabled: bool | None = None
    interval_s: int | None = Field(None, ge=1, le=86400)


@router.get("", response_model=list[HealthCheckInfo])
async def list_checks(db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    return await db.list_checks()


@router.post("/{name}/run", response_model=CheckResult)
async def run_check(name: str, scheduler=Depends(get_scheduler), _: Principal = Depends(require(Role.admin, Role.health))):
    if scheduler is None or name not in scheduler.checks:
        raise HTTPException(404, "Unknown check")
    return await scheduler.run_check(name)


@router.put("/{name}", response_model=HealthCheckInfo)
async def update_check(name: str, body: CheckUpdate, db: Database = Depends(get_db),
                       _: Principal = Depends(require(Role.admin))):
    if await db.get_check(name) is None:
        raise HTTPException(404, "Unknown check")
    return await db.update_check(name, enabled=body.enabled, interval_s=body.interval_s)
