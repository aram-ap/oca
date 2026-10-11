"""Slack incoming-webhook integration: outbound milestone messages, no inbound."""

from __future__ import annotations

import json
from typing import Any

import httpx

from oca.integrations.base import IntegrationError, TicketingIntegration
from oca.schemas import OutboundMessage, Ticket, TicketEvent

SLACK_DEFAULTS: dict[str, Any] = {"enabled": False, "webhook_url": "", "outbound_detail": "minimal"}
ICON = {"opened": ":red_circle:", "picked_up": ":mag:", "diagnosed": ":mag:", "awaiting_approval": ":large_yellow_circle:",
        "deploying": ":rocket:", "resolved": ":large_green_circle:", "needs_human": ":warning:",
        "report": ":page_facing_up:"}


class SlackIntegration(TicketingIntegration):
    name = "slack"
    defaults = SLACK_DEFAULTS

    def __init__(self, app) -> None:
        super().__init__(app)
        self.transport: httpx.AsyncBaseTransport | None = None

    def configured(self, config: dict[str, Any]) -> bool:
        return str(config.get("webhook_url", "")).startswith("https://")

    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]:
        config = await self.config()
        update = await self.describe(ticket, event, self.detail_of(config))
        if update is None or not update.text:
            return []
        text = f"{ICON.get(update.kind, ':speech_balloon:')} *{ticket.id}* {update.text}"
        return [OutboundMessage(target=self.name, kind="message", payload={"text": text}, ticket_id=ticket.id)]

    async def _post(self, url: str, text: str) -> int:
        data = json.dumps({"text": text}).encode()
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False, transport=self.transport) as client:
                response = await client.post(url, content=data, headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise IntegrationError(f"cannot reach Slack: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise IntegrationError(f"Slack webhook: HTTP {response.status_code} {response.text[:200]}")
        return len(data)

    async def deliver(self, item: dict[str, Any]) -> int:
        config = await self.config()
        if not self.configured(config):
            raise IntegrationError("Slack is not configured")
        if item["kind"] != "message":
            raise IntegrationError(f"unknown outbox kind {item['kind']!r}")
        sent = await self._post(config["webhook_url"], item["payload"]["text"])
        await self.record_sync(item["ticket_id"], item["kind"], sent)
        return sent

    async def test(self) -> dict[str, Any]:
        config = await self.config()
        if not self.configured(config):
            raise IntegrationError("an https:// incoming webhook URL is required")
        sent = await self._post(config["webhook_url"], ":white_check_mark: OCA connection test: this channel will receive incident updates.")
        return {"ok": True, "bytes_sent": sent}
