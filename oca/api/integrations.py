"""Ticketing integrations: connection status and a connection test per integration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oca.auth import Principal, current_principal, require
from oca.integrations import integration_worker
from oca.schemas import IntegrationStatus, Role

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("", response_model=list[IntegrationStatus])
async def list_integrations(request: Request, _: Principal = Depends(current_principal)):
    return await integration_worker(request.app).status()


@router.post("/{name}/test")
async def test_integration(name: str, request: Request,
                           _: Principal = Depends(require(Role.admin))) -> dict[str, Any]:
    integration = integration_worker(request.app).integrations.get(name)
    if integration is None:
        raise HTTPException(404, f"Unknown integration {name!r}")
    return await integration.check()
