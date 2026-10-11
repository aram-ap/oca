"""Background loops that connect OCA tickets to Jira and Slack (README §4.9).

Three loops run side by side in one task:

* listen  - every ticket event goes to each sending integration's ``on_event``; the
            messages it returns are written to ``integration_outbox``. No network here.
* send    - due outbox rows go out through ``deliver``. Failures retry with backoff and
            never block the pipeline. Rows for one ticket and target go out in order.
* poll    - integrations that read tickets (Jira) are polled on their interval; each new
            external ticket becomes one OCA ticket (``source`` = that system) and the
            agent pipeline starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from oca.integrations.base import DeliveryError, Integration
from oca.schemas import (
    Actor, EventType, InboundTicket, IntegrationStatus, OutboundDetail, OutboxStatus, TicketCreate,
    TicketEvent, TicketEventCreate, TicketSource, utcnow,
)

log = logging.getLogger("oca.integrations")

MAX_ATTEMPTS = 8              # 5 s, 10 s, 20 s ... capped at 5 min: about 15 minutes in total
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 300.0
DEFAULT_POLL_INTERVAL_S = 30.0


def backoff_s(attempts: int) -> float:
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** max(0, attempts - 1))


@dataclass
class IntegrationState:
    last_sync: datetime | None = None   # last successful poll or delivery
    last_error: str | None = None
    last_poll: float = float("-inf")    # time.monotonic() of the last poll attempt


class IntegrationWorker:
    def __init__(self, app: FastAPI, integrations: list[Integration]) -> None:
        self.app = app
        self.db = app.state.db
        self.integrations: dict[str, Integration] = {i.name: i for i in integrations}
        self.state: dict[str, IntegrationState] = {name: IntegrationState() for name in self.integrations}
        self._queue: asyncio.Queue | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> asyncio.Task:
        self._queue = self.app.state.bus.subscribe()  # before any loop runs, so no event is missed
        return asyncio.create_task(self._run(), name="oca-integrations")

    async def _run(self) -> None:
        try:
            await asyncio.gather(self._listen(), self._loop(self.send_due), self._loop(self.poll_due))
        finally:
            if self._queue is not None:
                self.app.state.bus.unsubscribe(self._queue)

    async def _loop(self, step) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken integration must not stop the others
                log.exception("integration %s failed", step.__name__)
            await asyncio.sleep(self.app.state.settings.integrations_tick_s)

    async def _listen(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("could not queue integration messages for event %s", event.id)

    # ------------------------------------------------------------------- outbound

    async def handle_event(self, event: TicketEvent) -> int:
        """Queue the messages each sending integration wants for this event. Returns how many."""
        if not self.integrations:
            return 0
        ticket = await self.db.get_ticket(event.ticket_id)
        if ticket is None:
            return 0
        queued = 0
        for name, integration in self.integrations.items():
            try:
                if not integration.sends(await integration.config()):
                    continue
                for message in await integration.on_event(ticket, event):
                    await self.db.enqueue_outbox(message)
                    queued += 1
            except Exception as exc:  # noqa: BLE001 - one integration's bug must not stop the others
                self.state[name].last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("%s could not build messages for %s", name, ticket.id)
        return queued

    async def send_due(self, limit: int = 20) -> int:
        """Send what is due. Returns how many were sent."""
        sent = 0
        for item in await self.db.due_outbox(limit):
            attempts = int(item["attempts"]) + 1
            target = item["target"]
            integration = self.integrations.get(target)
            try:
                if integration is None:
                    raise DeliveryError(f"no integration named {target!r}", retryable=False)
                if not integration.sends(await integration.config()):
                    raise DeliveryError(f"{target} is switched off or has no credentials")
                bytes_sent = await integration.deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded on the row; the pipeline carries on
                error = f"{type(exc).__name__}: {exc}"[:300]
                if getattr(exc, "retryable", True) and attempts < MAX_ATTEMPTS:
                    await self.db.mark_outbox(item["id"], OutboxStatus.pending, attempts,
                                              retry_in_s=backoff_s(attempts), error=error)
                else:
                    await self.db.mark_outbox(item["id"], OutboxStatus.failed, attempts, error=error)
                if target in self.state:
                    self.state[target].last_error = error
                log.warning("%s message %s for %s not sent (attempt %d): %s",
                            target, item["id"], item["ticket_id"], attempts, error)
                continue
            await self.db.mark_outbox(item["id"], OutboxStatus.sent, attempts, bytes_sent=int(bytes_sent or 0))
            self.state[target].last_sync, self.state[target].last_error = utcnow(), None
            sent += 1
        return sent

    # -------------------------------------------------------------------- inbound

    async def poll_due(self, force: bool = False) -> list[str]:
        """Poll integrations whose interval has passed. Returns the IDs of tickets created."""
        created: list[str] = []
        now = time.monotonic()
        for name, integration in self.integrations.items():
            cfg = await integration.config()
            if not integration.enabled(cfg):
                continue
            state = self.state[name]
            interval = float(cfg.get("poll_interval_s") or DEFAULT_POLL_INTERVAL_S)
            if not force and now - state.last_poll < interval:
                continue
            state.last_poll = now
            try:
                inbound = await integration.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - unreachable tracker: try again next interval
                state.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("%s poll failed: %s", name, state.last_error)
                continue
            state.last_sync, state.last_error = utcnow(), None
            for item in inbound:
                ticket_id = await self.import_ticket(item, cfg)
                if ticket_id is not None:
                    created.append(ticket_id)
        return created

    async def import_ticket(self, item: InboundTicket, cfg: dict[str, Any]) -> str | None:
        """Create the OCA ticket for an external ticket, once. Returns its ID, or None if it exists."""
        if await self.db.find_ticket_by_external_ref(item.system, item.key) is not None:
            return None
        settings = self.app.state.settings
        try:
            source, actor = TicketSource(item.system), Actor(item.system)
        except ValueError:
            source, actor = TicketSource.manual, Actor.system
        external_ref = {"system": item.system, "key": item.key, "url": item.url,
                        "last_synced_at": utcnow().isoformat()}
        service = item.service or cfg.get("default_service") or next(iter(settings.service_urls), "orders-api")
        ticket = await self.db.create_ticket(
            TicketCreate(source=source, service=service, title=item.title[:200], severity=item.severity,
                         description=item.description, external_ref=external_ref),
            TicketEventCreate(actor=actor, type=EventType.reported,
                              payload={"description": item.description or "", "title": item.title,
                                       "external_ref": external_ref}))
        log.info("imported %s %s as %s", item.system, item.key, ticket.id)
        pipeline = getattr(self.app.state, "pipeline", None)
        if pipeline is not None and settings.autostart_pipeline:
            pipeline.launch(ticket.id)
        return ticket.id

    # --------------------------------------------------------------------- status

    async def status(self) -> list[IntegrationStatus]:
        """Connection and queue state per integration, for the API and the dashboard."""
        stats = await self.db.outbox_stats()
        out = []
        for name, integration in self.integrations.items():
            cfg = await integration.config()
            state, counts = self.state[name], stats.get(name, {})
            out.append(IntegrationStatus(
                name=name, configured=integration.has_credentials(),
                connected=state.last_sync is not None and state.last_error is None,
                outbox_pending=counts.get("pending", 0), outbox_failed=counts.get("failed", 0),
                bytes_sent=counts.get("bytes_sent", 0), last_sync=state.last_sync,
                outbound_detail=integration.detail(cfg) if integration.sends(cfg) else OutboundDetail.off))
        return out
