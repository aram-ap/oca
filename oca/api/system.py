"""Liveness, LOCAL AI stats, settings, and the combined log view."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from oca.api.deps import get_db, get_settings
from oca.auth import Principal, current_principal, require
from oca.config import Settings
from oca.db import Database
from oca.integrations import integration_worker
from oca.schemas import LocalAIStats, OutboundDetail, Role, TicketEvent

router = APIRouter(tags=["system"])

# Keys operators may change at runtime and how they are validated.
SETTING_KEYS: dict[str, Any] = {
    "require_human_approval": bool,
    "risk_threshold": int,
    "baseline": dict,
    "thresholds": dict,
    "blocklist": list,
    "cooldown_s": int,
    "reopen_cooldown_s": int,
    "jira": dict,
    "slack": dict,
}


@router.get("/health")
async def health(settings: Settings = Depends(get_settings), db: Database = Depends(get_db)):
    await db.get_setting("require_human_approval")  # proves the DB answers
    return {"status": "ok", "service": "oca", "runner": settings.runner, "model": settings.model}


async def ollama_loaded(settings: Settings) -> bool | None:
    try:
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            response = await client.get(settings.ollama_url + "/api/ps")
            response.raise_for_status()
            return any(m.get("name") == settings.model or m.get("model") == settings.model
                       for m in response.json().get("models", []))
    except (httpx.HTTPError, ValueError):
        return None


@router.get("/stats/local-ai", response_model=LocalAIStats)
async def local_ai(request: Request, settings: Settings = Depends(get_settings), db: Database = Depends(get_db),
                   _: Principal = Depends(current_principal)):
    bytes_sent = await db.total_bytes_sent()
    sending = [s.name for s in await integration_worker(request.app).status()
               if s.outbound_detail is not OutboundDetail.off]
    if sending:
        mode = f"{', '.join(sending)} · metadata only · {bytes_sent} bytes external"
    else:
        mode = f"not connected · {bytes_sent} bytes external"
    return LocalAIStats(hardware=settings.hardware, model=settings.model, ticketing_mode=mode,
                        ticketing_bytes_sent=bytes_sent, ollama_url=settings.ollama_url,
                        loaded=await ollama_loaded(settings))


@router.get("/settings")
async def get_settings_view(db: Database = Depends(get_db), settings: Settings = Depends(get_settings),
                            _: Principal = Depends(current_principal)):
    stored = await db.all_settings()
    stored["runner"] = settings.runner
    stored["model"] = settings.model
    stored["fakeco_root"] = str(settings.fakeco_root)
    stored["allowlist_path"] = str(settings.allowlist_path)
    return stored


@router.put("/settings")
async def put_settings(changes: dict[str, Any], db: Database = Depends(get_db),
                       _: Principal = Depends(require(Role.admin))):
    for key, value in changes.items():
        expected = SETTING_KEYS.get(key)
        if expected is None:
            raise HTTPException(422, f"Unknown or read-only setting {key!r}")
        if expected is bool and not isinstance(value, bool):
            raise HTTPException(422, f"{key} must be a boolean")
        if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise HTTPException(422, f"{key} must be an integer")
        if expected in (dict, list) and not isinstance(value, expected):
            raise HTTPException(422, f"{key} must be a {expected.__name__}")
        if key == "risk_threshold" and not 0 <= value <= 100:
            raise HTTPException(422, "risk_threshold must be 0-100")
        await db.set_setting(key, value)
    return await db.all_settings()


@router.get("/logs", response_model=list[TicketEvent])
async def logs(ticket: str | None = None, after: int = 0, limit: int = Query(200, ge=1, le=2000),
               db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    """Ticket events across the system (service logs live under /api/tools/logs)."""
    if ticket:
        return await db.list_events(ticket, after_id=after, limit=limit)
    return await db.recent_events(limit=limit, after_id=after)
