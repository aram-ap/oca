"""Shared shape for ticketing integrations (README 4.9).

Inbound: ``poll()`` turns external issues into local tickets. Outbound: ``on_event()``
turns a local ticket event into messages for the ``integration_outbox``; ``deliver()``
sends one queued message. Every outbound text is built from a template over
whitelisted structured fields. Agent free text only appears at the ``summary``
detail level, and then only the Investigator's one-line root cause (D8).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from oca.schemas import (
    Actor, EventType, InboundTicket, MTTR, OutboundDetail, OutboundMessage, Ticket, TicketEvent,
    TicketEventCreate, TicketStatus,
)


class IntegrationError(RuntimeError):
    """Delivery failed; the outbox retries with backoff and eventually marks the item failed."""


class DeferDelivery(IntegrationError):
    """Not deliverable yet (for example the issue is still being created); retry soon, no attempt spent."""


SECRET_FIELDS: dict[str, tuple[str, ...]] = {"jira": ("token",), "slack": ("webhook_url",)}
MASK = "••••••••"
ONE_LINER_CHARS = 300


def mask_secrets(name: str, config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    for key in SECRET_FIELDS.get(name, ()):
        if out.get(key):
            out[key] = MASK
    return out


def merge_secrets(name: str, new: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """A masked or blank secret in ``new`` keeps the stored value."""
    merged = {**stored, **new}
    for key in SECRET_FIELDS.get(name, ()):
        if new.get(key) in (None, "", MASK):
            merged[key] = stored.get(key, "")
    return merged


def dashboard_link(public_url: str, ticket_id: str) -> str:
    return f"{public_url.rstrip('/')}/tickets/{ticket_id}"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s:02d}s"
    hours, m = divmod(minutes, 60)
    return f"{hours}h {m:02d}m"


def mttr_line(mttr: MTTR) -> str:
    parts = [f"{label} {duration(value)}" for label, value in (
        ("detection", mttr.detection_s), ("diagnosis", mttr.diagnosis_s), ("fix", mttr.fix_s),
        ("validation", mttr.validation_s), ("deployment", mttr.deployment_s)) if value is not None]
    return f"MTTR {duration(mttr.total_s)}" + (f" ({', '.join(parts)})" if parts else "")


def one_liner(text: Any, limit: int = ONE_LINER_CHARS) -> str:
    """Collapse agent prose to one bounded line (summary level only)."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass
class Update:
    """What happened on a ticket, expressed with whitelisted fields only."""

    kind: str                       # opened | picked_up | diagnosed | awaiting_approval | deploying | resolved | needs_human | status
    text: str | None                # human-readable line, or None when only the status moved
    status: TicketStatus | None     # the new ticket status, if the event was a status change


class TicketingIntegration(ABC):
    name: str
    defaults: dict[str, Any] = {}

    def __init__(self, app) -> None:
        self.app = app
        self.db = app.state.db
        self.settings = app.state.settings

    # ------------------------------------------------------------- configuration

    async def config(self) -> dict[str, Any]:
        stored = await self.db.get_setting(self.name, {}) or {}
        return {**self.defaults, **stored}

    @abstractmethod
    def configured(self, config: dict[str, Any]) -> bool: ...

    async def enabled(self) -> bool:
        config = await self.config()
        return bool(config.get("enabled")) and self.configured(config)

    @staticmethod
    def detail_of(config: dict[str, Any]) -> OutboundDetail:
        try:
            return OutboundDetail(config.get("outbound_detail", "minimal"))
        except ValueError:
            return OutboundDetail.minimal

    # ------------------------------------------------------------------ inbound

    async def poll(self) -> list[InboundTicket]:
        return []

    def parse_webhook(self, body: Any) -> InboundTicket | None:
        return None

    # ----------------------------------------------------------------- outbound

    @abstractmethod
    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]: ...

    @abstractmethod
    async def deliver(self, item: dict[str, Any]) -> int:
        """Send one outbox row. Returns the number of bytes sent. Raises IntegrationError to retry."""

    @abstractmethod
    async def test(self) -> dict[str, Any]:
        """Check credentials and configuration against the live system; raises IntegrationError."""

    # ------------------------------------------------------------------ helpers

    def link(self, ticket_id: str) -> str:
        return dashboard_link(self.settings.public_url, ticket_id)

    async def describe(self, ticket: Ticket, event: TicketEvent, detail: OutboundDetail) -> Update | None:
        """Template the event. Returns None when nothing should be sent."""
        if detail is OutboundDetail.off:
            return None
        link = self.link(ticket.id)
        payload = event.payload or {}
        if event.type is EventType.detected:
            return Update("opened", f"{ticket.id} opened for {ticket.service} ({ticket.severity.value}): {ticket.title}. "
                                    f"Dashboard: {link}", None)
        if event.type is EventType.reported:
            return Update("picked_up", f"OCA opened {ticket.id} for this issue and is investigating. Dashboard: {link}", None)
        if event.type is EventType.diagnosis:
            diagnosis = payload.get("diagnosis") or {}
            confidence = diagnosis.get("confidence")
            conf = f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "n/a"
            if detail is OutboundDetail.summary and diagnosis.get("diagnosis"):
                return Update("diagnosed", f"Root cause (confidence {conf}): {one_liner(diagnosis['diagnosis'])} "
                                           f"Details: {link}", None)
            return Update("diagnosed", f"Root cause identified (confidence {conf}). Details on the OCA dashboard: {link}", None)
        if event.type is EventType.status_change:
            try:
                status = TicketStatus(payload.get("to"))
            except ValueError:
                return None
            if status is TicketStatus.awaiting_approval:
                action = await self.db.latest_action(ticket.id)
                risk = action.evidence.risk_score if action and action.evidence.risk_score is not None else "n/a"
                return Update("awaiting_approval", f"Fix validated in sandbox, risk {risk}/100. Awaiting approval on the "
                                                   f"OCA dashboard: {link}", status)
            if status is TicketStatus.deploying:
                return Update("deploying", f"Approved. Deploying the fix to {ticket.service}. {link}", status)
            if status is TicketStatus.resolved:
                return Update("resolved", f"Resolved. {mttr_line(await self.db.mttr(ticket.id))}. {link}", status)
            if status is TicketStatus.denied:
                return Update("needs_human", f"Denied by a human reviewer; nothing was deployed. {link}", status)
            if status is TicketStatus.rolled_back:
                return Update("needs_human", f"Deployment rolled back; {ticket.service} is back on the previous version. {link}",
                              status)
            if status is TicketStatus.failed:
                return Update("needs_human", f"OCA could not produce a validated fix; a human is needed. {link}", status)
            return Update("status", None, status)
        return None

    async def record_sync(self, ticket_id: str, kind: str, bytes_sent: int, **extra: Any) -> None:
        await self.db.append_event(ticket_id, TicketEventCreate(
            actor=Actor(self.name), type=EventType.external_sync,
            payload={"target": self.name, "kind": kind, "bytes": bytes_sent, **extra}))
