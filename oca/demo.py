"""Dashboard "Restart test": put the company back to baseline and ship the demo incident again.

The runner does the work (``ops/reset.sh`` then ``ops/inject.sh <incident>`` in real mode, an in-memory
reset + inject in dry mode). It redeploys the stack twice, so it runs as a background task and the page
polls ``reinject_state`` until it finishes. OCA's own health checks then pick the incident up as usual.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from oca.schemas import TicketStatus

log = logging.getLogger("oca.demo")

INCIDENTS = ("timeout", "loyalty")
SERVICE = "orders-api"


class ReinjectBusy(RuntimeError):
    pass


def reinject_state(app) -> dict[str, Any]:
    return getattr(app.state, "reinject", None) or {"status": "idle"}


async def start_reinject(app, incident: str) -> dict[str, Any]:
    if incident not in INCIDENTS:
        raise ValueError(f"unknown incident {incident!r}")
    if reinject_state(app).get("status") == "running":
        raise ReinjectBusy("a restart is already running")
    state: dict[str, Any] = {"status": "running", "incident": incident, "steps": [], "error": None,
                             "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
    app.state.reinject = state
    app.state.reinject_task = asyncio.create_task(_run(app, state), name=f"reinject-{incident}")
    return state


async def wait_reinject(app) -> dict[str, Any]:
    task = getattr(app.state, "reinject_task", None)
    if task is not None:
        await task
    return reinject_state(app)


async def _run(app, state: dict[str, Any]) -> None:
    try:
        state["steps"] = await app.state.runner.reinject(state["incident"])
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            await scheduler.tick(force=True)
        state["status"] = "done"
        log.info("demo incident %r re-injected", state["incident"])
    except asyncio.CancelledError:
        state["status"], state["error"] = "failed", "cancelled"
        raise
    except Exception as exc:  # the page shows the reason; nothing else depends on this task
        state["status"], state["error"] = "failed", str(exc)
        log.error("re-inject of %r failed: %s", state["incident"], exc)
    finally:
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


async def reinject_status(app) -> dict[str, Any]:
    """The task state plus what would stop a new ticket from opening (shown on the page)."""
    db = app.state.db
    state = dict(reinject_state(app))
    blocking = await db.blocking_ticket_for_service(SERVICE)
    state["blocking_ticket"] = ({"id": blocking.id, "status": blocking.status.value}
                                if blocking is not None and blocking.status is not TicketStatus.resolved else None)
    cooldown = int(await db.get_setting("reopen_cooldown_s", 120) or 0)
    last = await db.last_resolved_at(SERVICE)
    remaining = cooldown - (datetime.now(timezone.utc) - last).total_seconds() if last else 0
    state["cooldown_remaining_s"] = max(0, int(remaining))
    return state
