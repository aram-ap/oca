import pytest

from oca.db import Database
from oca.ids import format_id, parse_id
from oca.schemas import (
    Actor, CheckResult, EventType, TicketCreate, TicketEventCreate, TicketStatus,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path):
    database = await Database(tmp_path / "t.db").connect()
    try:
        yield database
    finally:
        await database.close()


def test_ids():
    assert format_id("INC", 42) == "INC-0042"
    assert parse_id("DEP-12345") == ("DEP", 12345)
    with pytest.raises(ValueError):
        parse_id("nope")


async def test_ticket_lifecycle_and_events(db):
    created = TicketCreate(source="health_check", service="orders-api", title="errors up",
                           metrics_at_detect={"error_rate_pct": 25.0})
    first = TicketEventCreate(actor=Actor.health_check, type=EventType.detected, payload={"x": 1})
    ticket = await db.create_ticket(created, first)
    assert ticket.id == "INC-0001" and ticket.status is TicketStatus.detected
    assert (await db.create_ticket(created)).id == "INC-0002"

    queue = db.bus.subscribe(ticket.id)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator, note="go")
    live = queue.get_nowait()
    assert live.type is EventType.status_change and live.payload["to"] == "investigating"

    full = await db.get_ticket_with_events(ticket.id)
    assert [e.type for e in full.events] == [EventType.detected, EventType.status_change]
    assert (await db.open_ticket_for_service("orders-api")).id == "INC-0002"  # newest open
    assert await db.list_tickets(status=TicketStatus.investigating) == [await db.get_ticket(ticket.id)]

    await db.set_status(ticket.id, TicketStatus.resolved, Actor.executor)
    assert (await db.get_ticket(ticket.id)).resolved_at is not None
    assert (await db.last_resolved_at("orders-api")) is not None


async def test_checks_settings_and_counters(db):
    info = await db.register_check("status_check", "orders-api", 10)
    assert info.enabled and info.last_result is None
    await db.update_check("status_check", enabled=False, interval_s=30)
    again = await db.register_check("status_check", "orders-api", 10)
    assert (again.enabled, again.interval_s) == (False, 30)  # operator settings survive re-registration
    await db.record_check_result("status_check", CheckResult(ok=False, check="status_check", service="orders-api",
                                                              detail="bad", metrics={"e": 1.0}))
    assert (await db.get_check("status_check")).last_result.detail == "bad"

    await db.seed_setting("k", 1)
    await db.seed_setting("k", 2)
    assert await db.get_setting("k") == 1
    await db.set_setting("k", 3)
    assert await db.get_setting("k") == 3
    assert await db.next_id("DEP") == "DEP-0001"
    assert await db.next_id("DEP") == "DEP-0002"
