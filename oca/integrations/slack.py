"""Slack: one thread per OCA ticket, one short reply per step (README §4.9, decision D8).

Outbound only. The first message for a ticket starts a thread in the configured channel;
every later update replies in that thread. Important moments (waiting for approval,
resolved, failed) are also shown in the channel itself.

What a message may contain: the ticket ID, status, severity, service, the reporting
system's ticket key (e.g. Jira KAN-7), time to fix, a dashboard link and, only at
``outbound_detail: summary``, the Guardian's risk score. Never titles, descriptions, logs,
code, diffs, metrics or text written by agents.

Settings (dashboard, ``settings.slack``): ``enabled``, ``channel_id``, ``outbound_detail``.
Credentials (``.env``): ``OCA_SLACK_BOT_TOKEN``, a bot token with the ``chat:write`` scope.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from oca.integrations.base import DeliveryError, Integration
from oca.schemas import EventType, OutboundDetail, OutboundMessage, Ticket, TicketEvent, TicketStatus

if TYPE_CHECKING:
    from fastapi import FastAPI

API_URL = "https://slack.com/api/chat.postMessage"
TIMEOUT_S = 10.0

# Slack "ok": false errors that retrying cannot fix: wrong token, scope, or channel.
PERMANENT_ERRORS = frozenset({
    "invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "no_permission",
    "missing_scope", "channel_not_found", "not_in_channel", "is_archived", "restricted_action",
    "msg_too_long", "no_text", "invalid_arguments", "invalid_blocks", "team_access_not_granted",
})

# What to say when a ticket moves to a status: (text, also show in the channel).
# Statuses missing here (detected, gate, resolved) are covered by other events or skipped.
STATUS_MESSAGES: dict[TicketStatus, tuple[str, bool]] = {
    TicketStatus.investigating: (":mag: Investigating", False),
    TicketStatus.fix_proposed: (":hammer_and_wrench: Diagnosis complete, fix proposed", False),
    TicketStatus.sandbox_validated: (":test_tube: Fix passed in the sandbox", False),
    TicketStatus.reviewed: (":clipboard: Fix reviewed", False),
    TicketStatus.awaiting_approval: (":raised_hand: Waiting for approval on the OCA dashboard", True),
    TicketStatus.approved: (":white_check_mark: Approved", False),
    TicketStatus.deploying: (":rocket: Deploying the fix", False),
    TicketStatus.denied: (":no_entry: Fix denied, needs a person", True),
    TicketStatus.rolled_back: (":leftwards_arrow_with_hook: Deployment rolled back, needs a person", True),
    TicketStatus.failed: (":x: OCA could not fix this, needs a person", True),
}
LINKED_STATUSES = frozenset({TicketStatus.awaiting_approval, TicketStatus.denied,
                             TicketStatus.rolled_back, TicketStatus.failed})

SOURCE_NAMES = {"jira": "Jira", "slack": "Slack"}


def escape(text: Any) -> str:
    """Slack treats &, < and > as markup; escape anything not written by us."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown time"
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


class SlackIntegration(Integration):
    name = "slack"
    defaults = {"enabled": False, "channel_id": "", "outbound_detail": OutboundDetail.minimal.value}

    def __init__(self, app: "FastAPI", transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(app)
        self._transport = transport  # tests pass a fake Slack here

    # ------------------------------------------------------------------ configuration

    def has_credentials(self) -> bool:
        return bool(self.app.state.settings.slack_bot_token)

    def enabled(self, cfg: dict[str, Any]) -> bool:
        return super().enabled(cfg) and bool(str(cfg.get("channel_id") or "").strip())

    # ----------------------------------------------------------------------- messages

    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]:
        text, broadcast = await self.text_for(ticket, event)
        if text is None:
            return []
        return [self.message(ticket, "message", {"text": text, "broadcast": broadcast})]

    async def text_for(self, ticket: Ticket, event: TicketEvent) -> tuple[str | None, bool]:
        """The message for one event, or (None, False) when this event is not worth a message."""
        p = event.payload
        link = f"<{self.dashboard_link(ticket.id)}|Open {ticket.id} in OCA>"

        if event.type in (EventType.detected, EventType.reported):
            ref = ticket.external_ref or {}
            if ref.get("key"):
                system = SOURCE_NAMES.get(ref.get("system"), escape(ref.get("system")))
                origin = f"Reported in {system} {escape(ref.get('key'))}"
            elif event.type is EventType.detected:
                origin = "Detected by OCA health checks"
            else:
                origin = "Reported on the OCA dashboard"
            return (f":rotating_light: *{ticket.id}* · {escape(ticket.service)} · severity "
                    f"*{ticket.severity.value}*\n{origin}. Updates follow in this thread. {link}"), False

        if event.type is EventType.status_change:
            try:
                status = TicketStatus(p.get("to"))
            except ValueError:
                return None, False
            if status not in STATUS_MESSAGES:
                return None, False
            text, broadcast = STATUS_MESSAGES[status]
            if status is TicketStatus.reviewed and self.detail(await self.config()) is OutboundDetail.summary:
                review = await self.app.state.db.latest_event(ticket.id, EventType.review)
                risk = ((review.payload.get("review") or {}) if review else {}).get("risk_score")
                if isinstance(risk, int):
                    text += f", risk {risk}/100"
            if status in LINKED_STATUSES:
                text += f". {link}"
            return f"*{ticket.id}* {text}", broadcast

        if event.type is EventType.sandbox_result:
            if p.get("phase") == "before":
                text = ("Sandbox reproduced the incident" if p.get("reproduced")
                        else "Sandbox could not reproduce the incident")
                return f"*{ticket.id}* :test_tube: {text}", False
            if p.get("phase") == "after" and not p.get("passed"):
                return f"*{ticket.id}* :test_tube: Fix did not pass in the sandbox, trying again", False
            return None, False

        if event.type is EventType.resolved:
            total = (p.get("mttr") or {}).get("total_s")
            return f"*{ticket.id}* :white_check_mark: Resolved in {duration(total)}. {link}", True

        return None, False

    # ------------------------------------------------------------------------ sending

    async def deliver(self, item: dict[str, Any]) -> int:
        """Post one message: start the ticket's thread, or reply in it. Returns bytes sent."""
        db, ticket_id = self.app.state.db, item["ticket_id"]
        payload = item["payload"]
        link = await db.get_link(ticket_id, self.name)
        body: dict[str, Any] = {"text": payload["text"], "unfurl_links": False, "unfurl_media": False}
        if link and link.get("thread_ts"):
            body.update(channel=link["channel"], thread_ts=link["thread_ts"])
            if payload.get("broadcast"):
                body["reply_broadcast"] = True
        else:
            body["channel"] = str((await self.config()).get("channel_id") or "").strip()

        data = json.dumps(body).encode("utf-8")
        result = await self._post(data)
        if not link:
            await db.set_link(ticket_id, self.name, {"channel": result.get("channel") or body["channel"],
                                                     "thread_ts": result["ts"]})
        return len(data)

    async def _post(self, data: bytes) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.app.state.settings.slack_bot_token}",
                   "Content-Type": "application/json; charset=utf-8"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_S) as client:
                response = await client.post(API_URL, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"cannot reach Slack: {type(exc).__name__}") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(f"Slack HTTP {response.status_code}")
        if response.status_code >= 400:
            raise DeliveryError(f"Slack HTTP {response.status_code}", retryable=False)
        try:
            result = response.json()
        except ValueError:
            raise DeliveryError("Slack returned something that is not JSON") from None
        if not result.get("ok"):
            error = str(result.get("error") or "unknown_error")
            raise DeliveryError(f"Slack error: {error}", retryable=error not in PERMANENT_ERRORS)
        if not result.get("ts"):
            raise DeliveryError("Slack did not return a message timestamp")
        return result
