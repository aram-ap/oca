"""The shape every ticketing integration (Jira, Slack) follows (README §4.9, decision D8).

An integration does three things, and only the worker calls them:

* ``poll()``      inbound: new tickets in the external system -> ``InboundTicket`` list.
* ``on_event()``  outbound: a local ticket event -> messages to queue. Pure templating, no
                  network: it runs inside the event listener and must never block it.
* ``deliver()``   sends one queued message and returns the bytes that left the box.

Each integration builds its outbound text from whitelisted ticket fields only (ticket ID, status,
severity, service, risk score, time to fix, dashboard link). Logs, code, diffs, metrics
series and free text written by agents are never sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oca.schemas import InboundTicket, OutboundDetail, OutboundMessage, Ticket, TicketEvent

if TYPE_CHECKING:
    from fastapi import FastAPI


class DeliveryError(Exception):
    """Sending failed. ``retryable=False`` means trying again cannot help (bad token, bad channel)."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Integration:
    """Base class. Subclasses set ``name`` and ``defaults`` and override what they support."""

    name: str = ""
    defaults: dict[str, Any] = {"enabled": False, "outbound_detail": OutboundDetail.minimal.value}

    def __init__(self, app: "FastAPI") -> None:
        self.app = app

    # ----------------------------------------------------------------- configuration

    async def config(self) -> dict[str, Any]:
        """Operator settings (``settings.<name>`` in the DB) over this integration's defaults."""
        stored = await self.app.state.db.get_setting(self.name, {}) or {}
        return {**self.defaults, **stored}

    def has_credentials(self) -> bool:
        """True when the secrets this integration needs are set in the environment."""
        return False

    def missing_credentials(self) -> list[str]:
        """Names of the .env settings still to fill in, for the dashboard."""
        return []

    def enabled(self, cfg: dict[str, Any]) -> bool:
        """Switched on in settings and able to log in. Gates reading (``poll``)."""
        return bool(cfg.get("enabled")) and self.has_credentials()

    def sends(self, cfg: dict[str, Any]) -> bool:
        """Enabled and allowed to write out. Gates ``on_event``; ``outbound_detail: off`` stops it."""
        return self.enabled(cfg) and self.detail(cfg) is not OutboundDetail.off

    @staticmethod
    def detail(cfg: dict[str, Any]) -> OutboundDetail:
        try:
            return OutboundDetail(cfg.get("outbound_detail", OutboundDetail.minimal.value))
        except ValueError:
            return OutboundDetail.minimal

    # ------------------------------------------------------------------- the three calls

    async def poll(self) -> list[InboundTicket]:
        return []

    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]:
        return []

    async def deliver(self, item: dict[str, Any]) -> int:
        raise DeliveryError(f"{self.name} cannot send messages", retryable=False)

    async def check(self) -> dict[str, Any]:
        """Test the connection for the dashboard: {"ok": bool, "detail": str, "lines": [...]}."""
        return {"ok": False, "detail": f"{self.name} has no connection test", "lines": []}

    # ------------------------------------------------------------------------ helpers

    def dashboard_link(self, ticket_id: str) -> str:
        return f"{self.app.state.settings.dashboard_url}/tickets/{ticket_id}"

    def message(self, ticket: Ticket, kind: str, payload: dict[str, Any]) -> OutboundMessage:
        return OutboundMessage(target=self.name, kind=kind, payload=payload, ticket_id=ticket.id)
