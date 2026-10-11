"""Ticketing integrations: status, connection tests, and the outbox ledger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oca.api.deps import get_db
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.integrations import IntegrationError, integration_status, save_integration_settings
from oca.schemas import Role

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _integration(request: Request, name: str):
    integration = getattr(request.app.state, "integrations", {}).get(name)
    if integration is None:
        raise HTTPException(404, f"Unknown integration {name!r}")
    return integration


@router.get("")
async def status(request: Request, _: Principal = Depends(current_principal)):
    """Masked configuration, enabled state, outbox counters and the Jira reader check."""
    return await integration_status(request.app)


@router.put("/{name}")
async def configure(name: str, changes: dict[str, Any], request: Request, _: Principal = Depends(require(Role.admin))):
    _integration(request, name)
    if not isinstance(changes, dict):
        raise HTTPException(422, "Expected an object of settings")
    return await save_integration_settings(request.app, name, changes)


@router.post("/{name}/test")
async def test(name: str, request: Request, _: Principal = Depends(require(Role.admin))):
    """Check credentials against the live system (Jira: user, project, statuses; Slack: posts a test line)."""
    integration = _integration(request, name)
    try:
        return await integration.test()
    except IntegrationError as exc:
        raise HTTPException(502, str(exc))


@router.get("/outbox")
async def outbox(limit: int = 50, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    return await db.list_outbox(limit=max(1, min(limit, 500)))
