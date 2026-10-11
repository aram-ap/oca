"""Orchestrator-only sandbox lifecycle: prepare (reproduce), validate (prove), teardown."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from oca.api.deps import get_db, get_sandbox
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.fakeco.runner import RunnerError
from oca.sandbox import SandboxError
from oca.schemas import Actor, EventType, Role, SandboxRun, TicketEventCreate

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


async def _wrap(exc: Exception, db: Database, ticket_id: str, step: str) -> HTTPException:
    if isinstance(exc, SandboxError):
        return HTTPException(409, str(exc))
    detail = f"sandbox {step} failed: {exc}"
    if isinstance(exc, RunnerError) and exc.stderr:
        detail += f" | stderr: {exc.stderr.strip()[-600:]}"
    try:
        await db.append_event(ticket_id, TicketEventCreate(actor=Actor.system, type=EventType.error,
                                                           payload={"where": f"sandbox.{step}", "error": detail[:1500],
                                                                    "cmd": getattr(exc, "cmd", None), "rc": getattr(exc, "rc", None)}))
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass
    return HTTPException(502, detail[:1500])


@router.post("/{ticket_id}/prepare", response_model=SandboxRun)
async def prepare(ticket_id: str, sandbox=Depends(get_sandbox), db: Database = Depends(get_db),
                  _: Principal = Depends(require(Role.orchestrator))):
    try:
        return await sandbox.prepare(ticket_id)
    except (SandboxError, RunnerError, Exception) as exc:  # noqa: BLE001 - surfaced on the ticket, never swallowed
        raise await _wrap(exc, db, ticket_id, "prepare")


@router.post("/{ticket_id}/validate", response_model=SandboxRun)
async def validate(ticket_id: str, sandbox=Depends(get_sandbox), db: Database = Depends(get_db),
                   _: Principal = Depends(require(Role.orchestrator))):
    try:
        return await sandbox.validate(ticket_id)
    except (SandboxError, RunnerError, Exception) as exc:  # noqa: BLE001
        raise await _wrap(exc, db, ticket_id, "validate")


@router.post("/{ticket_id}/teardown")
async def teardown(ticket_id: str, sandbox=Depends(get_sandbox), _: Principal = Depends(require(Role.orchestrator))):
    await sandbox.teardown(ticket_id)
    return {"ticket_id": ticket_id, "sandbox": "down"}


@router.get("/{ticket_id}", response_model=SandboxRun | None)
async def latest(ticket_id: str, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    return await db.latest_sandbox_run(ticket_id)
