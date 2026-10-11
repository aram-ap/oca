"""Orchestrator-only sandbox lifecycle: prepare (reproduce), validate (prove), teardown."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from oca.api.deps import get_db, get_sandbox
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.fakeco.runner import RunnerError
from oca.sandbox import SandboxError
from oca.schemas import Role, SandboxRun

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


def _wrap(exc: Exception) -> HTTPException:
    if isinstance(exc, SandboxError):
        return HTTPException(409, str(exc))
    return HTTPException(502, f"sandbox operation failed: {exc}")


@router.post("/{ticket_id}/prepare", response_model=SandboxRun)
async def prepare(ticket_id: str, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.orchestrator))):
    try:
        return await sandbox.prepare(ticket_id)
    except (SandboxError, RunnerError) as exc:
        raise _wrap(exc)


@router.post("/{ticket_id}/validate", response_model=SandboxRun)
async def validate(ticket_id: str, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.orchestrator))):
    try:
        return await sandbox.validate(ticket_id)
    except (SandboxError, RunnerError) as exc:
        raise _wrap(exc)


@router.post("/{ticket_id}/teardown")
async def teardown(ticket_id: str, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.orchestrator))):
    await sandbox.teardown(ticket_id)
    return {"ticket_id": ticket_id, "sandbox": "down"}


@router.get("/{ticket_id}", response_model=SandboxRun | None)
async def latest(ticket_id: str, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    return await db.latest_sandbox_run(ticket_id)
