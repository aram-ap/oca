"""Integration foundation: outbox queueing, ordered delivery with retries, inbound import."""

import pytest

from oca.config import Settings
from oca.integrations import DeliveryError, Integration, IntegrationWorker
from oca.schemas import (
    Actor, EventType, InboundTicket, OutboundDetail, Severity, TicketCreate, TicketEventCreate,
    TicketSource, TicketStatus,
)

pytestmark = pytest.mark.anyio


class FakeChat(Integration):
    """Stands in for Slack: one message per event, records what it 'sends'."""

    name = "slack"

    def __init__(self, app, failures=0, retryable=True):
        super().__init__(app)
        self.sent, self.failures, self.retryable = [], failures, retryable

    def has_credentials(self):
        return True

    async def on_event(self, ticket, event):
        return [self.message(ticket, "message", {"text": f"{ticket.id} {event.type.value}"})]

    async def deliver(self, item):
        if self.failures:
            self.failures -= 1
            raise DeliveryError("chat is down", retryable=self.retryable)
        self.sent.append(item["payload"]["text"])
        return len(item["payload"]["text"])


class FakeTracker(Integration):
    """Stands in for Jira inbound: returns the same open issue on every poll."""

    name = "jira"

    def has_credentials(self):
        return True

    async def poll(self):
        return [InboundTicket(system="jira", key="KAN-7", url="https://example.atlassian.net/browse/KAN-7",
                              title="Checkout is failing", description="customers see errors",
                              severity=Severity.high)]


async def _ticket(db):
    return await db.create_ticket(
        TicketCreate(source=TicketSource.manual, service="orders-api", title="t"),
        TicketEventCreate(actor=Actor.human, type=EventType.reported, payload={}))


async def _make_due(db):
    """Pretend the retry wait has passed."""
    await db.conn.execute("UPDATE integration_outbox SET next_attempt = '2000-01-01T00:00:00+00:00'")


async def test_switched_off_integration_queues_nothing(app):
    worker = IntegrationWorker(app, [FakeChat(app)])
    ticket = await _ticket(app.state.db)
    event = (await app.state.db.list_events(ticket.id))[0]
    assert await worker.handle_event(event) == 0


async def test_event_is_queued_then_sent_and_bytes_counted(app):
    db = app.state.db
    await db.set_setting("slack", {"enabled": True})
    chat = FakeChat(app)
    worker = IntegrationWorker(app, [chat])
    ticket = await _ticket(db)
    assert await worker.handle_event((await db.list_events(ticket.id))[0]) == 1
    assert await worker.send_due() == 1
    assert chat.sent == [f"{ticket.id} reported"]
    stats = (await db.outbox_stats())["slack"]
    assert stats == {"pending": 0, "failed": 0, "bytes_sent": len(chat.sent[0])}
    assert (await worker.status())[0].connected


async def test_failed_send_retries_and_later_messages_wait_their_turn(app):
    db = app.state.db
    await db.set_setting("slack", {"enabled": True})
    chat = FakeChat(app, failures=1)
    worker = IntegrationWorker(app, [chat])
    ticket = await _ticket(db)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator)
    for event in await db.list_events(ticket.id):
        await worker.handle_event(event)

    assert await worker.send_due() == 0          # first fails; second must not overtake it
    assert chat.sent == []
    assert (await db.outbox_stats())["slack"]["pending"] == 2

    await _make_due(db)
    assert await worker.send_due() == 1          # the retry goes out first...
    assert await worker.send_due() == 1          # ...then the message that was waiting
    assert chat.sent == [f"{ticket.id} reported", f"{ticket.id} status_change"]


async def test_permanent_failure_is_marked_failed_and_does_not_block(app):
    db = app.state.db
    await db.set_setting("slack", {"enabled": True})
    chat = FakeChat(app, failures=1, retryable=False)
    worker = IntegrationWorker(app, [chat])
    ticket = await _ticket(db)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator)
    for event in await db.list_events(ticket.id):
        await worker.handle_event(event)

    assert await worker.send_due() == 0
    assert (await db.outbox_stats())["slack"]["failed"] == 1
    assert await worker.send_due() == 1
    assert chat.sent == [f"{ticket.id} status_change"]


async def test_switched_off_after_queueing_retries_instead_of_sending(app):
    db = app.state.db
    await db.set_setting("slack", {"enabled": True})
    chat = FakeChat(app)
    worker = IntegrationWorker(app, [chat])
    ticket = await _ticket(db)
    await worker.handle_event((await db.list_events(ticket.id))[0])
    await db.set_setting("slack", {"enabled": False})
    assert await worker.send_due() == 0
    assert chat.sent == [] and (await db.outbox_stats())["slack"]["pending"] == 1


async def test_outbound_detail_off_stops_sending(app):
    await app.state.db.set_setting("slack", {"enabled": True, "outbound_detail": "off"})
    worker = IntegrationWorker(app, [FakeChat(app)])
    ticket = await _ticket(app.state.db)
    assert await worker.handle_event((await app.state.db.list_events(ticket.id))[0]) == 0
    assert (await worker.status())[0].outbound_detail is OutboundDetail.off


async def test_poll_imports_each_external_ticket_once(app):
    db = app.state.db
    await db.set_setting("jira", {"enabled": True})
    worker = IntegrationWorker(app, [FakeTracker(app)])
    created = await worker.poll_due(force=True)
    assert len(created) == 1
    assert await worker.poll_due(force=True) == []           # same issue again: no second ticket

    ticket = await db.get_ticket(created[0])
    assert ticket.source is TicketSource.jira and ticket.severity is Severity.high
    assert ticket.external_ref["key"] == "KAN-7"
    first = (await db.list_events(ticket.id))[0]
    assert first.type is EventType.reported and first.actor is Actor.jira
    assert (await db.find_ticket_by_external_ref("jira", "KAN-7")).id == ticket.id


class NewIssueEachPoll(FakeTracker):
    def __init__(self, app):
        super().__init__(app)
        self.count = 0

    async def poll(self):
        self.count += 1
        return [InboundTicket(system="jira", key=f"KAN-{self.count}", title=f"issue {self.count}")]


async def test_poll_respects_interval_and_switch(app):
    db = app.state.db
    tracker = NewIssueEachPoll(app)
    worker = IntegrationWorker(app, [tracker])
    assert await worker.poll_due(force=True) == [] and tracker.count == 0   # switched off
    await db.set_setting("jira", {"enabled": True, "poll_interval_s": 3600})
    assert len(await worker.poll_due()) == 1
    assert await worker.poll_due() == [] and tracker.count == 1             # interval not reached yet
    assert len(await worker.poll_due(force=True)) == 1


async def test_failed_poll_is_recorded_and_retried_next_interval(app):
    class Down(FakeTracker):
        async def poll(self):
            raise ConnectionError("tracker unreachable")

    await app.state.db.set_setting("jira", {"enabled": True})
    worker = IntegrationWorker(app, [Down(app)])
    assert await worker.poll_due(force=True) == []
    assert "unreachable" in worker.state["jira"].last_error
    assert not (await worker.status())[0].connected


async def test_links_are_stored_per_ticket_and_target(app):
    db = app.state.db
    ticket = await _ticket(db)
    assert await db.get_link(ticket.id, "slack") is None
    await db.set_link(ticket.id, "slack", {"channel": "C1", "thread_ts": "1.0"})
    await db.set_link(ticket.id, "slack", {"channel": "C1", "thread_ts": "2.0"})
    assert await db.get_link(ticket.id, "slack") == {"channel": "C1", "thread_ts": "2.0"}
    assert await db.get_link(ticket.id, "jira") is None


async def test_worker_is_started_with_the_app(app):
    assert isinstance(app.state.integrations, IntegrationWorker)


def test_credentials_come_from_env_and_stay_out_of_repr():
    settings = Settings.from_env(env={"OCA_SLACK_BOT_TOKEN": "xoxb-secret", "OCA_JIRA_API_TOKEN": "ATATT-secret",
                                      "OCA_JIRA_EMAIL": "me@example.com", "OCA_JIRA_URL": "https://x.atlassian.net/",
                                      "OCA_PUBLIC_URL": "http://192.168.1.20:8001/"}, dotenv=None)
    assert settings.slack_bot_token == "xoxb-secret" and settings.jira_api_token == "ATATT-secret"
    assert settings.jira_url == "https://x.atlassian.net"
    assert settings.dashboard_url == "http://192.168.1.20:8001"
    shown = repr(settings)
    assert "xoxb-secret" not in shown and "ATATT-secret" not in shown and "me@example.com" not in shown
    assert Settings().dashboard_url == "http://localhost:8001"   # falls back to the API address
