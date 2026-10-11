"""Slack: thread per ticket, message content policy, error handling. Uses a fake Slack."""

import dataclasses
import json

import httpx
import pytest

from oca.integrations import IntegrationWorker, SlackIntegration, build_integrations, start_integrations
from oca.integrations.slack import duration, escape
from oca.main import create_app, shutdown, startup
from oca.schemas import (
    Actor, EventType, Severity, TicketCreate, TicketEventCreate, TicketSource, TicketStatus,
)

pytestmark = pytest.mark.anyio

TOKEN = "xoxb-test-token"


class FakeSlack:
    """Answers chat.postMessage like Slack does and records every request."""

    def __init__(self):
        self.requests, self.replies, self.count = [], [], 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.count += 1
        body = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": f"1700.{self.count:04d}"})

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
async def slack_app(settings):
    app = create_app(dataclasses.replace(settings, slack_bot_token=TOKEN,
                                         public_url="http://192.168.1.20:8001"))
    # The app's own background worker would post to the real Slack; each test drives its own worker.
    app.state.service_starters = [s for s in app.state.service_starters if s is not start_integrations]
    await startup(app)
    try:
        yield app
    finally:
        await shutdown(app)


@pytest.fixture
def fake():
    return FakeSlack()


@pytest.fixture
async def worker(slack_app, fake):
    await slack_app.state.db.set_setting("slack", {"enabled": True, "channel_id": "C123"})
    return IntegrationWorker(slack_app, [SlackIntegration(slack_app, transport=httpx.MockTransport(fake.handler))])


async def _jira_ticket(db):
    return await db.create_ticket(
        TicketCreate(source=TicketSource.jira, service="orders-api", title="SECRET-TITLE checkout down",
                     description="SECRET-DESCRIPTION customer card 4111", severity=Severity.high,
                     external_ref={"system": "jira", "key": "KAN-7", "url": "https://x.atlassian.net/browse/KAN-7"}),
        TicketEventCreate(actor=Actor.jira, type=EventType.reported,
                          payload={"title": "SECRET-TITLE", "description": "SECRET-DESCRIPTION"}))


async def _flush(worker, db, ticket_id):
    """Queue every event of the ticket, then send until the queue is empty."""
    for event in await db.list_events(ticket_id):
        await worker.handle_event(event)
    while await worker.send_due():
        pass


async def test_first_message_starts_a_thread_and_updates_reply_in_it(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator)
    await db.set_status(ticket.id, TicketStatus.fix_proposed, Actor.orchestrator)
    await _flush(worker, db, ticket.id)

    root, *replies = fake.bodies()
    assert root["channel"] == "C123" and "thread_ts" not in root
    assert len(replies) == 2 and all(r["thread_ts"] == "1700.0001" and r["channel"] == "C123" for r in replies)
    assert await db.get_link(ticket.id, "slack") == {"channel": "C123", "thread_ts": "1700.0001"}
    assert "Investigating" in replies[0]["text"] and "fix proposed" in replies[1]["text"]


async def test_messages_carry_only_whitelisted_fields(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    await _flush(worker, db, ticket.id)
    text = fake.bodies()[0]["text"]
    assert ticket.id in text and "orders-api" in text and "high" in text and "Jira KAN-7" in text
    assert f"http://192.168.1.20:8001/tickets/{ticket.id}" in text
    for secret in ("SECRET", "4111", "checkout down"):
        assert secret not in text
    assert all(TOKEN not in json.dumps(b) for b in fake.bodies())
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_health_check_ticket_says_it_was_detected(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await db.create_ticket(
        TicketCreate(source=TicketSource.health_check, service="orders-api", title="errors up"),
        TicketEventCreate(actor=Actor.health_check, type=EventType.detected, payload={"metrics": {}}))
    await _flush(worker, db, ticket.id)
    assert "Detected by OCA health checks" in fake.bodies()[0]["text"]


async def test_important_steps_are_also_shown_in_the_channel(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    await db.set_status(ticket.id, TicketStatus.deploying, Actor.executor)
    await db.set_status(ticket.id, TicketStatus.awaiting_approval, Actor.gate)
    await db.append_event(ticket.id, TicketEventCreate(actor=Actor.executor, type=EventType.resolved,
                                                       payload={"mttr": {"total_s": 82.0}}))
    await _flush(worker, db, ticket.id)
    _, deploying, approval, resolved = fake.bodies()
    assert "reply_broadcast" not in deploying
    assert approval["reply_broadcast"] is True and "/tickets/" in approval["text"]
    assert resolved["reply_broadcast"] is True and "Resolved in 1m 22s" in resolved["text"]


async def test_quiet_events_send_nothing(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    await db.set_status(ticket.id, TicketStatus.gate, Actor.orchestrator)
    await db.set_status(ticket.id, TicketStatus.resolved, Actor.executor)   # the resolved event carries the message
    await db.append_event(ticket.id, TicketEventCreate(actor=Actor.investigator, type=EventType.diagnosis,
                                                       payload={"diagnosis": {"root_cause": "SECRET"}}))
    await _flush(worker, db, ticket.id)
    assert len(fake.requests) == 1   # only the thread root


async def test_sandbox_results(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    for payload in ({"phase": "before", "reproduced": True}, {"phase": "after", "passed": False},
                    {"phase": "after", "passed": True}):
        await db.append_event(ticket.id, TicketEventCreate(actor=Actor.orchestrator, type=EventType.sandbox_result,
                                                           payload=payload))
    await _flush(worker, db, ticket.id)
    texts = [b["text"] for b in fake.bodies()[1:]]
    assert len(texts) == 2
    assert "reproduced the incident" in texts[0] and "did not pass" in texts[1]


async def test_risk_score_only_at_summary_detail(slack_app, worker, fake):
    db = slack_app.state.db
    ticket = await _jira_ticket(db)
    await db.append_event(ticket.id, TicketEventCreate(actor=Actor.guardian, type=EventType.review,
                                                       payload={"review": {"risk_score": 8, "decision": "APPROVE"}}))
    await db.set_status(ticket.id, TicketStatus.reviewed, Actor.orchestrator)
    await _flush(worker, db, ticket.id)
    assert "risk" not in fake.bodies()[-1]["text"]

    await db.set_setting("slack", {"enabled": True, "channel_id": "C123", "outbound_detail": "summary"})
    await db.set_status(ticket.id, TicketStatus.reviewed, Actor.orchestrator)
    event = (await db.list_events(ticket.id))[-1]
    await worker.handle_event(event)
    await worker.send_due()
    assert "risk 8/100" in fake.bodies()[-1]["text"]


async def test_wrong_channel_fails_permanently(slack_app, worker, fake):
    db = slack_app.state.db
    fake.replies.append(httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    ticket = await _jira_ticket(db)
    await _flush(worker, db, ticket.id)
    assert (await db.outbox_stats())["slack"]["failed"] == 1
    assert "channel_not_found" in worker.state["slack"].last_error


@pytest.mark.parametrize("reply", [
    httpx.Response(200, json={"ok": False, "error": "ratelimited"}),
    httpx.Response(429, json={"ok": False, "error": "ratelimited"}),
    httpx.Response(503, text="unavailable"),
    httpx.ConnectError("no network"),
])
async def test_temporary_problems_retry_later(slack_app, worker, fake, reply):
    db = slack_app.state.db
    fake.replies.append(reply)
    ticket = await _jira_ticket(db)
    await _flush(worker, db, ticket.id)
    assert (await db.outbox_stats())["slack"] == {"pending": 1, "failed": 0, "bytes_sent": 0}
    assert await db.get_link(ticket.id, "slack") is None


async def test_switched_on_needs_a_channel_and_a_token(slack_app, settings):
    db = slack_app.state.db
    slack = SlackIntegration(slack_app)
    await db.set_setting("slack", {"enabled": True})
    assert not slack.enabled(await slack.config())                  # no channel
    await db.set_setting("slack", {"enabled": True, "channel_id": "C123"})
    assert slack.enabled(await slack.config())

    no_token = create_app(settings)
    assert not SlackIntegration(no_token).has_credentials()


async def test_slack_is_registered(slack_app):
    assert "slack" in [i.name for i in build_integrations(slack_app)]


def test_helpers():
    assert duration(82) == "1m 22s" and duration(7) == "7s" and duration(3700) == "1h 1m"
    assert duration(None) == "unknown time"
    assert escape("<a&b>") == "&lt;a&amp;b&gt;"
