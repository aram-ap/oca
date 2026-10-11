import pytest

from oca.schemas import EventType, Role, TicketStatus

pytestmark = pytest.mark.anyio


async def test_detection_opens_exactly_one_ticket(app, client, auth):
    scheduler, runner, db = app.state.scheduler, app.state.runner, app.state.db
    checks = {c["name"]: c for c in (await client.get("/api/checks", headers=auth(Role.admin))).json()}
    assert checks["status_check"]["enabled"] and not checks["jira_ticket_reader"]["enabled"]

    results = await scheduler.tick(force=True)
    assert all(r.ok for r in results) and await db.list_tickets() == []

    runner.inject("timeout")
    await scheduler.tick(force=True)
    tickets = await db.list_tickets()
    assert len(tickets) == 1 and tickets[0].status is TicketStatus.detected
    assert tickets[0].severity.value == "critical" and tickets[0].metrics_at_detect["error_rate_pct"] > 20
    events = await db.list_events(tickets[0].id)
    assert events[0].type is EventType.detected and events[0].payload["check"] == "status_check"
    assert events[0].payload["status"]["db_errors"] == {"timeout:bestsellers_30d": 7.1}

    await scheduler.tick(force=True)
    assert len(await db.list_tickets()) == 1
    types = [e.type for e in await db.list_events(tickets[0].id)]
    assert types.count(EventType.check_failed) == 0  # still inside the one-per-minute window
    scheduler._last_failed_event[tickets[0].id] = 0.0  # pretend a minute passed
    await scheduler.tick(force=True)
    await scheduler.tick(force=True)
    types = [e.type for e in await db.list_events(tickets[0].id)]
    assert types.count(EventType.check_failed) == 1  # rate limited to one per minute

    # Run-now endpoint reports the live failure and respects roles.
    assert (await client.post("/api/checks/status_check/run", headers=auth(Role.engineer))).status_code == 403
    run = await client.post("/api/checks/status_check/run", headers=auth(Role.admin))
    assert run.status_code == 200 and run.json()["ok"] is False and "INCIDENT" in run.json()["detail"]

    # Resolve and keep failing: the reopen cooldown prevents a duplicate.
    await db.set_status(tickets[0].id, TicketStatus.resolved, actor="executor")
    await scheduler.tick(force=True)
    assert len(await db.list_tickets()) == 1

    disabled = await client.put("/api/checks/status_check", json={"enabled": False, "interval_s": 5}, headers=auth(Role.admin))
    assert disabled.json()["enabled"] is False and disabled.json()["interval_s"] == 5


async def test_failed_ticket_does_not_cause_a_storm(app):
    scheduler, runner, db = app.state.scheduler, app.state.runner, app.state.db
    runner.inject("timeout")
    await scheduler.tick(force=True)
    ticket = (await db.list_tickets())[0]
    await db.set_status(ticket.id, TicketStatus.failed, actor="orchestrator")
    for _ in range(3):
        await scheduler.tick(force=True)
    assert len(await db.list_tickets()) == 1  # a failed ticket needs a human, not a twin


async def test_baseline_learning(app):
    scheduler, db = app.state.scheduler, app.state.db
    before = await db.get_setting("baseline")
    for _ in range(5):
        await scheduler.run_check("status_check")
    after = await db.get_setting("baseline")
    assert after != before and 10 < after["p95_latency_ms"] < 20


async def test_connection_errors_are_confirmed_before_opening_a_ticket(app, monkeypatch):
    """One refused connection (a redeploy restarting the service) must not open a ticket; two in a row do."""
    scheduler, runner, db = app.state.scheduler, app.state.runner, app.state.db

    async def refused(service):
        return (False, "ConnectError: All connection attempts failed") if service == "orders-api" else (True, "HTTP 200")

    monkeypatch.setattr(runner, "health", refused)
    await scheduler.tick(force=True)
    assert await db.list_tickets() == []
    await scheduler.tick(force=True)
    tickets = await db.list_tickets()
    assert len(tickets) == 1 and tickets[0].title.startswith("orders-api: ConnectError")
    events = await db.list_events(tickets[0].id)
    assert events[0].payload["check"] == "endpoint_check:orders-api" and events[0].payload["breaches"] == ["endpoint"]

    # A single blip after recovery resets the count.
    monkeypatch.undo()
    await db.set_status(tickets[0].id, TicketStatus.resolved, actor="executor")
    await db.set_setting("reopen_cooldown_s", 0)
    await scheduler.tick(force=True)
    monkeypatch.setattr(runner, "health", refused)
    await scheduler.tick(force=True)
    assert len(await db.list_tickets()) == 1
