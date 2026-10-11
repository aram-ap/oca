"""POST /api/actions (the gate) plus human approve/deny."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from oca.api.deps import get_db
from oca.auth import Principal, require
from oca.db import Database
from oca.gate import GateContext, evaluate
from oca.schemas import (
    Action, ActionRequest, ActionStatus, Actor, DeploymentStatus, EventType, GateDecision, OPEN_STATUSES,
    Review, Role, TicketEventCreate, TicketStatus,
)

router = APIRouter(tags=["actions"])


class DecisionIn(BaseModel):
    note: str | None = None


async def build_context(request: Request, db: Database, ticket, evidence) -> GateContext:
    app = request.app
    run = None
    if evidence.sandbox_run_id:
        run = await db.get_sandbox_run(evidence.sandbox_run_id)
        if run is not None and run.ticket_id != ticket.id:
            run = None  # evidence from another ticket does not count
    if run is None:
        run = await db.latest_sandbox_run(ticket.id)
    review = None
    review_event = await db.latest_event(ticket.id, EventType.review)
    if review_event is not None:
        try:
            review = Review.model_validate(review_event.payload.get("review", review_event.payload))
        except Exception:  # noqa: BLE001 - malformed review counts as no review
            review = None
    cooldown = int(await db.get_setting("cooldown_s", 120) or 0)
    last = await db.last_deployment_for_service(ticket.service)
    if last is not None and last.status in (DeploymentStatus.failed,):
        last = None
    return GateContext(
        allowlist=app.state.allowlist, sandbox_run=run, review=review, last_deployment=last,
        require_human_approval=bool(await db.get_setting("require_human_approval", False)),
        risk_threshold=int(await db.get_setting("risk_threshold", 30)),
        cooldown_s=cooldown, blocklist=list(await db.get_setting("blocklist", []) or []),
    )


async def approve_and_execute(request: Request, db: Database, action: Action, decided_by: str, actor: Actor):
    ticket = await db.get_ticket(action.ticket_id)
    action = await db.update_action(action.id, ActionStatus.approved, decided_by=decided_by)
    deployment = await db.create_deployment(action.ticket_id, action.action, action.args,
                                            evidence={"action_id": action.id, **action.evidence.model_dump(mode="json")},
                                            note=f"{action.ticket_id} via {decided_by}")
    await db.update_action(action.id, ActionStatus.approved, deployment_id=deployment.id)
    await db.update_ticket(action.ticket_id, current_deployment_id=deployment.id)
    await db.set_status(action.ticket_id, TicketStatus.approved, actor, action_id=action.id, deployment_id=deployment.id)
    request.app.state.executor.launch(deployment.id)
    return deployment


@router.post("/actions", response_model=GateDecision)
async def submit_action(body: ActionRequest, request: Request, db: Database = Depends(get_db),
                        principal: Principal = Depends(require(Role.orchestrator))):
    ticket = await db.get_ticket(body.ticket_id)
    if ticket is None:
        raise HTTPException(404, "Unknown ticket")
    if ticket.status not in OPEN_STATUSES or ticket.status in (TicketStatus.awaiting_approval, TicketStatus.approved,
                                                                TicketStatus.deploying):
        raise HTTPException(409, f"Ticket is {ticket.status.value}; no new action accepted")
    body.requested_by = principal.actor
    ctx = await build_context(request, db, ticket, body.evidence)
    decision = evaluate(body, ctx)
    action = await db.create_action(body, decision)
    decision.action_id = action.id
    await db.set_status(ticket.id, TicketStatus.gate, Actor.orchestrator, action_id=action.id)
    await db.append_event(ticket.id, TicketEventCreate(actor=principal.actor, type=EventType.action_request,
                                                       payload={"action_id": action.id, **body.model_dump(mode="json")}))
    await db.append_event(ticket.id, TicketEventCreate(actor=Actor.gate, type=EventType.gate_decision,
                                                       payload=decision.model_dump(mode="json")))
    await db.update_ticket(ticket.id, pending_action_id=action.id)
    if decision.status is ActionStatus.denied:
        await db.set_status(ticket.id, TicketStatus.denied, Actor.gate, note=decision.reason)
    elif decision.requires_human:
        await db.update_action(action.id, ActionStatus.awaiting_approval)
        await db.set_status(ticket.id, TicketStatus.awaiting_approval, Actor.gate, action_id=action.id)
    else:
        await approve_and_execute(request, db, action, decided_by="gate", actor=Actor.gate)
    return decision


async def _pending_action(db: Database, ticket_id: str) -> tuple[object, Action]:
    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Unknown ticket")
    action = await db.get_action(ticket.pending_action_id) if ticket.pending_action_id else await db.latest_action(ticket_id)
    if action is None:
        raise HTTPException(409, "No action is waiting on this ticket")
    return ticket, action


@router.post("/tickets/{ticket_id}/approve")
async def approve(ticket_id: str, request: Request, body: DecisionIn | None = None, db: Database = Depends(get_db),
                  principal: Principal = Depends(require(Role.admin))):
    ticket, action = await _pending_action(db, ticket_id)
    if action.status not in (ActionStatus.awaiting_approval, ActionStatus.denied, ActionStatus.policy_pass):
        raise HTTPException(409, f"Action {action.id} is {action.status.value}; nothing to approve")
    override = action.status is ActionStatus.denied
    await db.append_event(ticket_id, TicketEventCreate(actor=Actor.human, type=EventType.human_decision,
                                                       payload={"decision": "approve", "action_id": action.id, "override": override,
                                                                "by": principal.name, "note": body.note if body else None}))
    deployment = await approve_and_execute(request, db, action, decided_by=f"human:{principal.name}", actor=Actor.human)
    return {"ticket_id": ticket_id, "action_id": action.id, "deployment_id": deployment.id, "override": override}


@router.post("/tickets/{ticket_id}/deny")
async def deny(ticket_id: str, body: DecisionIn | None = None, db: Database = Depends(get_db),
               principal: Principal = Depends(require(Role.admin))):
    ticket, action = await _pending_action(db, ticket_id)
    if action.status not in (ActionStatus.awaiting_approval, ActionStatus.policy_pass):
        raise HTTPException(409, f"Action {action.id} is {action.status.value}; nothing to deny")
    await db.update_action(action.id, ActionStatus.denied, decided_by=f"human:{principal.name}", reason="denied by operator")
    await db.append_event(ticket_id, TicketEventCreate(actor=Actor.human, type=EventType.human_decision,
                                                       payload={"decision": "deny", "action_id": action.id, "by": principal.name,
                                                                "note": body.note if body else None}))
    await db.set_status(ticket_id, TicketStatus.denied, Actor.human, note=body.note if body else None)
    return {"ticket_id": ticket_id, "action_id": action.id, "status": "denied"}


@router.post("/tickets/{ticket_id}/confirm")
async def confirm(ticket_id: str, request: Request, body: DecisionIn | None = None, db: Database = Depends(get_db),
                  principal: Principal = Depends(require(Role.admin))):
    """A deployment held for verification (improved but still breaching) is confirmed as the resolution."""
    try:
        dep = await request.app.state.executor.confirm(ticket_id, f"human:{principal.name}", (body.note if body else None) or "")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ticket_id": ticket_id, "deployment_id": dep.id, "status": "resolved"}


@router.post("/tickets/{ticket_id}/rollback")
async def rollback_held(ticket_id: str, request: Request, body: DecisionIn | None = None, db: Database = Depends(get_db),
                        principal: Principal = Depends(require(Role.admin))):
    """A deployment held for verification is rolled back to its backup."""
    try:
        dep = await request.app.state.executor.reject(ticket_id, f"human:{principal.name}", (body.note if body else None) or "")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ticket_id": ticket_id, "deployment_id": dep.id, "status": "rolled_back"}


@router.get("/tickets/{ticket_id}/action", response_model=Action)
async def current_action(ticket_id: str, db: Database = Depends(get_db), _: Principal = Depends(require(
        Role.admin, Role.orchestrator, Role.investigator, Role.engineer, Role.guardian, Role.health))):
    _, action = await _pending_action(db, ticket_id)
    return action
