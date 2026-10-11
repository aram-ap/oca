"""In-process fan-out of ticket events to SSE subscribers."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from oca.schemas import TicketEvent

QUEUE_LIMIT = 500


class EventBus:
    """Per-ticket and global subscriber queues. Publishing never blocks or raises."""

    def __init__(self) -> None:
        self._by_ticket: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._global: set[asyncio.Queue] = set()

    def subscribe(self, ticket_id: str | None = None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        (self._global if ticket_id is None else self._by_ticket[ticket_id]).add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue, ticket_id: str | None = None) -> None:
        if ticket_id is None:
            self._global.discard(queue)
        else:
            self._by_ticket[ticket_id].discard(queue)
            if not self._by_ticket[ticket_id]:
                del self._by_ticket[ticket_id]

    def publish(self, event: TicketEvent) -> None:
        for queue in list(self._by_ticket.get(event.ticket_id, ())) + list(self._global):
            if queue.full():
                try:
                    queue.get_nowait()  # drop the oldest; the SSE replay-on-reconnect covers gaps
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def subscriber_count(self) -> int:
        return len(self._global) + sum(len(s) for s in self._by_ticket.values())
