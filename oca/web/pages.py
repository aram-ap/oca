"""Dashboard pages and htmx fragments. All state comes from the same DB the API serves."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from oca.api import actions as actions_api
from oca.api import deployments as deployments_api
from oca.api.system import ollama_loaded
from oca.api.tickets import SSE_HEADERS, event_stream
from oca.auth import Principal
from oca.schemas import Actor, EventType, Role, TicketEvent, TicketStatus
from oca.web.common import TEMPLATES, is_fragment, render, web_user
from oca.web.topology import topology_context

router = APIRouter(dependencies=[Depends(web_user)])


def _user(request: Request) -> Principal:
    user = web_user(request)
    request.state.user = user
    return user


async def _gate_context(request: Request, ticket_id: str) -> dict[str, Any]:
    db = request.app.state.db
    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Unknown ticket")
    action = await db.get_action(ticket.pending_action_id) if ticket.pending_action_id else await db.latest_action(ticket_id)
    gate_event = await db.latest_event(ticket_id, EventType.gate_decision)
    review_event = await db.latest_event(ticket_id, EventType.review)
    run = await db.latest_sandbox_run(ticket_id)
    deployment = await db.get_deployment(ticket.current_deployment_id) if ticket.current_deployment_id else None
    mttr = await db.mttr(ticket_id) if ticket.status is TicketStatus.resolved else None
    return {"ticket": ticket, "action": action, "gate": gate_event.payload if gate_event else None,
            "review": (review_event.payload.get("review") if review_event else None), "run": run,
            "deployment": deployment, "mttr": mttr}


# ----------------------------------------------------------------------- dashboard


@router.get("/dash")
async def dash(request: Request, user: Principal = Depends(_user)):
    db, settings = request.app.state.db, request.app.state.settings
    tickets = await db.list_tickets(limit=10)
    open_tickets = [t for t in tickets if t.status.value not in ("resolved", "failed", "rolled_back")]
    checks = await db.list_checks()
    deployments = await db.list_deployments(limit=5)
    local_ai = await _local_ai(request)
    runner_state = _runner_state(request)
    return render(request, "pages/dash.html", tickets=tickets, open_tickets=open_tickets, checks=checks,
                  deployments=deployments, local_ai=local_ai,
                  dry=settings.runner == "dry", runner_state=runner_state,
                  topology=topology_context(checks, tickets, local_ai, runner_state, settings))


@router.get("/dash/topology")
async def topology_fragment(request: Request, user: Principal = Depends(_user)):
    db = request.app.state.db
    return render(request, "fragments/topology.html", topology=topology_context(
        await db.list_checks(), await db.list_tickets(limit=10),
        await _local_ai(request), _runner_state(request), request.app.state.settings), dry=request.app.state.settings.runner == "dry")


def _runner_state(request: Request) -> dict[str, Any] | None:
    runner = request.app.state.runner
    return runner.describe() if hasattr(runner, "describe") else None


async def _local_ai(request: Request) -> dict[str, Any]:
    db, settings = request.app.state.db, request.app.state.settings
    return {"hardware": settings.hardware, "model": settings.model, "bytes_sent": await db.total_bytes_sent(),
            "jira": await db.get_setting("jira", {}) or {}, "loaded": await ollama_loaded(settings)}


@router.get("/dash/local-ai")
async def local_ai_fragment(request: Request, user: Principal = Depends(_user)):
    return render(request, "fragments/local_ai_panel.html", local_ai=await _local_ai(request))


@router.post("/dash/inject")
async def inject(request: Request, incident: str = Form("timeout"), user: Principal = Depends(_user)):
    runner, settings = request.app.state.runner, request.app.state.settings
    if settings.runner != "dry" or not hasattr(runner, "inject"):
        raise HTTPException(404, "Inject is only available in dry mode")
    if incident not in ("timeout", "loyalty"):
        raise HTTPException(422, "Unknown incident")
    runner.inject(incident)
    scheduler = request.app.state.scheduler
    if scheduler is not None:
        await scheduler.tick(force=True)
    return RedirectResponse("/tickets", status_code=303)


@router.post("/dash/reset")
async def reset(request: Request, user: Principal = Depends(_user)):
    runner = request.app.state.runner
    if not hasattr(runner, "reset"):
        raise HTTPException(404, "Reset is only available in dry mode")
    runner.reset()
    return RedirectResponse("/dash", status_code=303)


# ------------------------------------------------------------------------- tickets


@router.get("/tickets")
async def tickets(request: Request, status: TicketStatus | None = None, user: Principal = Depends(_user)):
    rows = await request.app.state.db.list_tickets(status=status, limit=200)
    template = "fragments/ticket_table.html" if is_fragment(request) else "pages/tickets.html"
    return render(request, template, tickets=rows, status_filter=status)


@router.get("/tickets/{ticket_id}")
async def ticket_detail(request: Request, ticket_id: str, user: Principal = Depends(_user)):
    db = request.app.state.db
    ctx = await _gate_context(request, ticket_id)
    events = await db.list_events(ticket_id)
    return render(request, "pages/ticket_detail.html", events=events, last_event_id=events[-1].id if events else 0, **ctx)


@router.get("/tickets/{ticket_id}/timeline")
async def ticket_timeline(request: Request, ticket_id: str, after: int = 0, user: Principal = Depends(_user)):
    events = await request.app.state.db.list_events(ticket_id, after_id=after)
    return render(request, "fragments/timeline.html", events=events, ticket_id=ticket_id)


@router.get("/tickets/{ticket_id}/gate")
async def ticket_gate(request: Request, ticket_id: str, user: Principal = Depends(_user)):
    return render(request, "fragments/gate_status.html", **await _gate_context(request, ticket_id))


def _render_row(request: Request, event: TicketEvent) -> str:
    return TEMPLATES.get_template("fragments/timeline_row.html").render(e=event, request=request)


@router.get("/tickets/{ticket_id}/stream")
async def ticket_stream(request: Request, ticket_id: str, after: int = 0, limit: int | None = Query(None, ge=1),
                        timeout_s: float | None = Query(None, gt=0), user: Principal = Depends(_user)):
    """SSE of rendered timeline rows; status changes also push a refreshed gate panel."""
    app = request.app
    if await app.state.db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    def render_event(event: TicketEvent) -> str:
        html = _render_row(request, event).replace("\n", " ")
        out = f"event: event\nid: {event.id}\ndata: {html}\n\n"
        if event.type in (EventType.status_change, EventType.gate_decision, EventType.human_decision,
                          EventType.resolved, EventType.rolled_back):
            out += f"event: status\nid: {event.id}\ndata: <div id=\"gate-panel\" hx-get=\"/tickets/{ticket_id}/gate\" hx-trigger=\"load\" hx-swap=\"outerHTML\"></div>\n\n"
        return out

    return StreamingResponse(event_stream(request, app.state.db, app.state.bus, ticket_id, after, limit, timeout_s, render=render_event),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/tickets/{ticket_id}/approve")
async def approve(request: Request, ticket_id: str, note: str = Form(""), user: Principal = Depends(_user)):
    try:
        await actions_api.approve(ticket_id, request, actions_api.DecisionIn(note=note or None), request.app.state.db, user)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
    return render(request, "fragments/gate_status.html", **await _gate_context(request, ticket_id))


@router.post("/tickets/{ticket_id}/deny")
async def deny(request: Request, ticket_id: str, note: str = Form(""), user: Principal = Depends(_user)):
    try:
        await actions_api.deny(ticket_id, actions_api.DecisionIn(note=note or None), request.app.state.db, user)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
    return render(request, "fragments/gate_status.html", **await _gate_context(request, ticket_id))


@router.post("/tickets/{ticket_id}/run")
async def rerun(request: Request, ticket_id: str, user: Principal = Depends(_user)):
    if await request.app.state.db.get_ticket(ticket_id) is None:
        raise HTTPException(404, "Unknown ticket")
    request.app.state.pipeline.launch(ticket_id)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# --------------------------------------------------------------------- deployments


@router.get("/deployments")
async def deployments(request: Request, user: Principal = Depends(_user)):
    rows = await request.app.state.db.list_deployments(limit=200)
    template = "fragments/deployment_table.html" if is_fragment(request) else "pages/deployments.html"
    return render(request, template, deployments=rows)


@router.post("/deployments/{deployment_id}/restore")
async def restore(request: Request, deployment_id: str, user: Principal = Depends(_user)):
    await deployments_api.restore(deployment_id, request, deployments_api.RestoreIn(reason="Restore Backup clicked on dashboard"),
                                  request.app.state.db, user)
    rows = await request.app.state.db.list_deployments(limit=200)
    return render(request, "fragments/deployment_table.html", deployments=rows)


# ------------------------------------------------------------------------- health


@router.get("/health")
async def health(request: Request, user: Principal = Depends(_user)):
    checks = await request.app.state.db.list_checks()
    template = "fragments/check_table.html" if is_fragment(request) else "pages/health.html"
    return render(request, template, checks=checks)


@router.post("/health/{name}/run")
async def run_check(request: Request, name: str, user: Principal = Depends(_user)):
    scheduler = request.app.state.scheduler
    if scheduler is None or name not in scheduler.checks:
        raise HTTPException(404, "Unknown check")
    await scheduler.run_check(name)
    return render(request, "fragments/check_table.html", checks=await request.app.state.db.list_checks())


@router.post("/health/{name}/toggle")
async def toggle_check(request: Request, name: str, user: Principal = Depends(_user)):
    db = request.app.state.db
    info = await db.get_check(name)
    if info is None:
        raise HTTPException(404, "Unknown check")
    await db.update_check(name, enabled=not info.enabled)
    return render(request, "fragments/check_table.html", checks=await db.list_checks())


# --------------------------------------------------------------------------- logs


@router.get("/logs")
async def logs(request: Request, user: Principal = Depends(_user)):
    events = await request.app.state.db.recent_events(limit=300)
    return render(request, "pages/logs.html", events=list(reversed(events)))


# ----------------------------------------------------------------------- settings


@router.get("/settings")
async def settings_page(request: Request, user: Principal = Depends(_user)):
    db = request.app.state.db
    return render(request, "pages/settings.html", stored=await db.all_settings(),
                  allowlist=request.app.state.settings.allowlist_path.read_text(encoding="utf-8"), saved=False)


@router.post("/settings")
async def settings_save(request: Request, require_human_approval: str | None = Form(None),
                        risk_threshold: int = Form(30), cooldown_s: int = Form(120), blocklist: str = Form(""),
                        user: Principal = Depends(_user)):
    db = request.app.state.db
    await db.set_setting("require_human_approval", require_human_approval is not None)
    await db.set_setting("risk_threshold", max(0, min(100, risk_threshold)))
    await db.set_setting("cooldown_s", max(0, cooldown_s))
    await db.set_setting("blocklist", [b.strip() for b in blocklist.replace("\n", ",").split(",") if b.strip()])
    return render(request, "pages/settings.html", stored=await db.all_settings(),
                  allowlist=request.app.state.settings.allowlist_path.read_text(encoding="utf-8"), saved=True)
