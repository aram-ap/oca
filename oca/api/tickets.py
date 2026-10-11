"""Tickets, their event timelines, and the SSE stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from oca.api.deps import get_bus, get_db
from oca.auth import Principal, current_principal, require
from oca.db import Database
from oca.events import EventBus
from oca.schemas import (
    Actor, EventType, MTTR, Role, Ticket, TicketCreate, TicketEvent, TicketEventCreate,
    TicketSource, TicketStatus, TicketWithEvents,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])

# Status transitions the orchestrator may request. Gate/executor/human transitions
# happen server-side in actions.py and executor.py, never through this endpoint.
ORCHESTRATOR_STATUSES = frozenset({
    TicketStatus.investigating, TicketStatus.fix_proposed, TicketStatus.sandbox_validated,
    TicketStatus.reviewed, TicketStatus.gate, TicketStatus.failed,
})


class EventIn(BaseModel):
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: Actor | None = None  # honoured only for the orchestrator relaying an agent's output


RELAYABLE_ACTORS = frozenset({Actor.investigator, Actor.engineer, Actor.guardian, Actor.orchestrator})


class StatusIn(BaseModel):
    status: TicketStatus
    note: str | None = None
    attempt: int | None = None


@router.get("", response_model=list[Ticket])
async def list_tickets(status: TicketStatus | None = None, service: str | None = None,
                       open: bool = False, limit: int = Query(100, ge=1, le=1000),
                       db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    return await db.list_tickets(status=status, service=service, open_only=open, limit=limit)


@router.post("", response_model=Ticket, status_code=201)
async def create_ticket(data: TicketCreate, request: Request, db: Database = Depends(get_db),
                        principal: Principal = Depends(require(Role.admin, Role.health, Role.jira,
                                                               Role.slack, Role.orchestrator))):
    if data.source == TicketSource.health_check:
        first = TicketEventCreate(actor=Actor.health_check, type=EventType.detected,
                                  payload={"metrics": data.metrics_at_detect or {}, "title": data.title})
    else:
        first = TicketEventCreate(actor=principal.actor, type=EventType.reported,
                                  payload={"description": data.description or "", "title": data.title,
                                           "external_ref": data.external_ref})
    ticket = await db.create_ticket(data, first)
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is not None and request.app.state.settings.autostart_pipeline:
        pipeline.launch(ticket.id)
    return ticket


@router.get("/{ticket_id}", response_model=TicketWithEvents)
async def get_ticket(ticket_id: str, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    ticket = await db.get_ticket_with_events(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Unknown ticket")
    return ticket


@router.get("/{ticket_id}/events", response_model=list[TicketEvent])
async def list_events(ticket_id: str, after: int = 0, db: Database = Depends(get_db),
                      _: Principal = Depends(current_principal)):
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    return await db.list_events(ticket_id, after_id=after)


@router.post("/{ticket_id}/events", response_model=TicketEvent, status_code=201)
async def append_event(ticket_id: str, event: EventIn, db: Database = Depends(get_db),
                       principal: Principal = Depends(require(Role.investigator, Role.engineer,
                                                              Role.guardian, Role.orchestrator))):
    """Agents and the orchestrator append findings. The actor is always the caller's role."""
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    if event.type in {EventType.gate_decision, EventType.human_decision, EventType.run,
                      EventType.resolved, EventType.rolled_back, EventType.status_change}:
        raise HTTPException(403, f"Event type {event.type.value!r} is written by the platform, not by clients")
    actor = principal.actor
    if event.actor is not None and principal.has(Role.orchestrator) and event.actor in RELAYABLE_ACTORS:
        actor = event.actor
    return await db.append_event(ticket_id, TicketEventCreate(actor=actor, type=event.type, payload=event.payload))


@router.post("/{ticket_id}/run")
async def run_pipeline(ticket_id: str, request: Request, db: Database = Depends(get_db),
                       _: Principal = Depends(require(Role.admin))):
    """(Re)launch the agent pipeline for a ticket; used after a failure or when autostart is off."""
    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Unknown ticket")
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(503, "Pipeline is not available")
    task = pipeline.launch(ticket_id)
    return {"ticket_id": ticket_id, "running": not task.done()}


@router.post("/{ticket_id}/status", response_model=Ticket)
async def set_status(ticket_id: str, body: StatusIn, db: Database = Depends(get_db),
                     principal: Principal = Depends(require(Role.orchestrator))):
    if body.status not in ORCHESTRATOR_STATUSES and principal.role is not Role.admin:
        raise HTTPException(403, f"The orchestrator may not set status {body.status.value!r}")
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    if body.attempt is not None:
        await db.update_ticket(ticket_id, attempt=body.attempt)
    return await db.set_status(ticket_id, body.status, principal.actor, note=body.note)


@router.get("/{ticket_id}/report")
async def report(ticket_id: str, request: Request, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    """The incident report as Markdown, rendered from the ticket's events (also written to OCA_DATA_DIR/reports on close)."""
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    text = await request.app.state.reports.render(ticket_id)
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@router.get("/{ticket_id}/mttr", response_model=MTTR)
async def mttr(ticket_id: str, db: Database = Depends(get_db), _: Principal = Depends(current_principal)):
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    return await db.mttr(ticket_id)


def format_sse(event: TicketEvent, name: str = "event", data: str | None = None) -> str:
    body = data if data is not None else event.model_dump_json()
    lines = "".join(f"data: {line}\n" for line in body.splitlines() or [""])
    return f"event: {name}\nid: {event.id}\n{lines}\n"


async def event_stream(request: Request, db: Database, bus: EventBus, ticket_id: str | None,
                       after: int, limit: int | None, timeout_s: float | None,
                       render=format_sse):
    """Replay events after ``after`` from the DB, then stream live ones. Lossless on reconnect."""
    queue = bus.subscribe(ticket_id)
    sent = 0
    deadline = asyncio.get_running_loop().time() + timeout_s if timeout_s else None
    try:
        backlog = (await db.list_events(ticket_id, after_id=after) if ticket_id
                   else await db.recent_events(limit=200, after_id=after))
        last_id = after
        for event in backlog:
            yield render(event)
            last_id, sent = event.id, sent + 1
            if limit and sent >= limit:
                return
        while True:
            if await request.is_disconnected():
                return
            wait = 15.0
            if deadline is not None:
                wait = min(wait, deadline - asyncio.get_running_loop().time())
                if wait <= 0:
                    return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event.id <= last_id:
                continue
            yield render(event)
            last_id, sent = event.id, sent + 1
            if limit and sent >= limit:
                return
    finally:
        bus.unsubscribe(queue, ticket_id)


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


@router.get("/{ticket_id}/stream")
async def stream(ticket_id: str, request: Request, after: int = 0,
                 limit: int | None = Query(None, ge=1), timeout_s: float | None = Query(None, gt=0),
                 db: Database = Depends(get_db), bus: EventBus = Depends(get_bus),
                 _: Principal = Depends(current_principal)):
    if await db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return StreamingResponse(event_stream(request, db, bus, ticket_id, after, limit, timeout_s),
                             media_type="text/event-stream", headers=SSE_HEADERS)
