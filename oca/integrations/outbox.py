"""Event relay (ticket events -> outbox rows) and the delivery worker (outbox rows -> external systems).

The pipeline never waits on an external system: ``append_event`` publishes on the in-process
bus, the relay turns events into outbox rows, and the worker delivers them with retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from oca.integrations.base import DeferDelivery, IntegrationError, TicketingIntegration
from oca.schemas import EventType, OutboxStatus, TicketEvent

log = logging.getLogger("oca.integrations")

BACKOFF_S = (5.0, 15.0, 60.0, 300.0)
DEFER_S = 3.0
MAX_ATTEMPTS = len(BACKOFF_S) + 1
SKIP_EVENTS = frozenset({EventType.external_sync, EventType.finding, EventType.check_failed})


class EventRelay:
    def __init__(self, app, integrations: dict[str, TicketingIntegration]) -> None:
        self.app = app
        self.db = app.state.db
        self.integrations = integrations
        self.queue: asyncio.Queue = app.state.bus.subscribe()

    async def handle(self, event: TicketEvent) -> int:
        """Enqueue outbound messages for one event. Returns how many rows were queued."""
        if event.type in SKIP_EVENTS:
            return 0
        ticket = await self.db.get_ticket(event.ticket_id)
        if ticket is None:
            return 0
        queued = 0
        for integration in self.integrations.values():
            try:
                if not await integration.enabled():
                    continue
                for message in await integration.on_event(ticket, event):
                    await self.db.enqueue_outbox(message)
                    queued += 1
            except Exception:  # noqa: BLE001 - a broken integration must not stall the others
                log.exception("%s: on_event failed for %s/%s", integration.name, event.ticket_id, event.type.value)
        return queued

    async def run_forever(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("relay failed for event %s", event.id)
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        """Wait until every published event has been relayed (tests)."""
        await self.queue.join()


class OutboxWorker:
    def __init__(self, app, integrations: dict[str, TicketingIntegration], poll_s: float = 2.0,
                 max_attempts: int = MAX_ATTEMPTS) -> None:
        self.db = app.state.db
        self.integrations = integrations
        self.poll_s = poll_s
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()

    async def pump(self) -> int:
        """Deliver everything that is due. Returns the number of rows sent."""
        sent = 0
        async with self._lock:
            for item in await self.db.due_outbox():
                if await self._deliver(item):
                    sent += 1
        return sent

    async def _deliver(self, item: dict[str, Any]) -> bool:
        integration = self.integrations.get(item["target"])
        attempts = int(item.get("attempts") or 0)
        if integration is None:
            await self.db.mark_outbox(item["id"], OutboxStatus.failed, attempts + 1, error=f"no integration {item['target']!r}")
            return False
        try:
            bytes_sent = await integration.deliver(item)
        except DeferDelivery as exc:
            await self.db.mark_outbox(item["id"], OutboxStatus.pending, attempts, retry_in_s=DEFER_S, error=str(exc))
            return False
        except (IntegrationError, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            attempts += 1
            error = f"{type(exc).__name__}: {exc}"[:500]
            if attempts >= self.max_attempts:
                log.error("outbox %s to %s failed permanently: %s", item["id"], item["target"], error)
                await self.db.mark_outbox(item["id"], OutboxStatus.failed, attempts, error=error)
            else:
                delay = BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]
                log.warning("outbox %s to %s failed (attempt %d, retry in %.0fs): %s", item["id"], item["target"], attempts, delay, error)
                await self.db.mark_outbox(item["id"], OutboxStatus.pending, attempts, retry_in_s=delay, error=error)
            return False
        await self.db.mark_outbox(item["id"], OutboxStatus.sent, attempts + 1, bytes_sent=bytes_sent)
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("outbox pump failed")
            await asyncio.sleep(self.poll_s)
