"""Jira Cloud: inbound import, comments and transitions, issue creation, errors. Uses a fake Jira."""

import base64
import dataclasses
import json
import re

import httpx
import pytest

from oca.integrations import IntegrationWorker, JiraIntegration, build_integrations, start_integrations
from oca.integrations.jira import adf_to_text
from oca.main import create_app, shutdown, startup
from oca.schemas import (
    Actor, EventType, Severity, TicketCreate, TicketEventCreate, TicketSource, TicketStatus,
)

pytestmark = pytest.mark.anyio

URL, EMAIL, TOKEN = "https://team.atlassian.net", "me@example.com", "ATATT-test-token"


class FakeJira:
    """Just enough of the Jira Cloud REST API, with a board of To Do / In Progress / In Review / Done."""

    STATUSES = ["To Do", "In Progress", "In Review", "Done"]

    def __init__(self):
        self.requests, self.issues, self.comments, self.next_key = [], {}, [], 10
        self.search_results, self.fail_next = [], []

    def add_issue(self, key, summary="Checkout is failing", priority="High", components=("orders-api",),
                  labels=("oca",), description=None):
        self.issues[key] = "To Do"
        self.search_results.append({"key": key, "fields": {
            "summary": summary, "priority": {"name": priority} if priority else None,
            "components": [{"name": c} for c in components], "labels": list(labels),
            "description": description}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            failure = self.fail_next.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": self.search_results})
        if path == "/rest/api/2/issue" and method == "POST":
            key = f"KAN-{self.next_key}"
            self.next_key += 1
            self.issues[key] = "To Do"
            return httpx.Response(201, json={"id": "1", "key": key})
        m = re.fullmatch(r"/rest/api/2/issue/([A-Z]+-\d+)(/comment|/transitions)?", path)
        if not m or m.group(1) not in self.issues:
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        key, sub = m.group(1), m.group(2)
        if sub == "/comment":
            self.comments.append((key, body["body"]))
            return httpx.Response(201, json={"id": str(len(self.comments))})
        if sub == "/transitions" and method == "GET":
            return httpx.Response(200, json={"transitions": [
                {"id": str(i), "name": s, "to": {"name": s}} for i, s in enumerate(self.STATUSES)
                if s != self.issues[key]]})
        if sub == "/transitions":
            self.issues[key] = self.STATUSES[int(body["transition"]["id"])]
            return httpx.Response(204)
        return httpx.Response(200, json={"key": key, "fields": {"status": {"name": self.issues[key]}}})

    def posted(self):
        return [json.loads(r.content) for r in self.requests if r.content]


@pytest.fixture
async def jira_app(settings):
    app = create_app(dataclasses.replace(settings, jira_url=URL, jira_email=EMAIL, jira_api_token=TOKEN,
                                         public_url="http://192.168.1.20:8001"))
    # The app's own background worker would call the real Jira; each test drives its own worker.
    app.state.service_starters = [s for s in app.state.service_starters if s is not start_integrations]
    await startup(app)
    try:
        yield app
    finally:
        await shutdown(app)


@pytest.fixture
def fake():
    return FakeJira()


@pytest.fixture
async def worker(jira_app, fake):
    await jira_app.state.db.set_setting("jira", {"enabled": True, "project": "KAN"})
    return IntegrationWorker(jira_app, [JiraIntegration(jira_app, transport=httpx.MockTransport(fake.handler))])


async def _send_all(worker, db, ticket_id, after=0):
    for event in await db.list_events(ticket_id, after_id=after):
        await worker.handle_event(event)
    while await worker.send_due():
        pass


async def _health_ticket(db):
    return await db.create_ticket(
        TicketCreate(source=TicketSource.health_check, service="orders-api", title="SECRET error rate 38%",
                     severity=Severity.high, metrics_at_detect={"error_rate_pct": 38.0}),
        TicketEventCreate(actor=Actor.health_check, type=EventType.detected, payload={"metrics": {}}))


# ------------------------------------------------------------------------- inbound

async def test_poll_imports_labelled_issue_once_with_mapped_fields(jira_app, worker, fake):
    fake.add_issue("KAN-7", priority="Highest", components=("payments",), description={
        "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Cards declined"}]}]})
    created = await worker.poll_due(force=True)
    assert len(created) == 1 and await worker.poll_due(force=True) == []

    ticket = await jira_app.state.db.get_ticket(created[0])
    assert ticket.source is TicketSource.jira and ticket.severity is Severity.high
    assert ticket.service == "payments" and ticket.title == "Checkout is failing"
    assert ticket.description == "Cards declined"
    assert ticket.external_ref == {**ticket.external_ref, "system": "jira", "key": "KAN-7",
                                   "url": f"{URL}/browse/KAN-7"}

    search = fake.requests[0]
    jql = search.url.params["jql"]
    assert 'project = "KAN"' in jql and 'labels = "oca"' in jql and 'statusCategory = "To Do"' in jql
    expected = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert search.headers["authorization"] == expected


async def test_poll_skips_issues_oca_created_and_uses_default_service(jira_app, worker, fake):
    fake.add_issue("KAN-1", labels=("oca-auto",))
    fake.add_issue("KAN-2", components=(), priority=None)
    created = await worker.poll_due(force=True)
    assert len(created) == 1
    ticket = await jira_app.state.db.get_ticket(created[0])
    assert ticket.external_ref["key"] == "KAN-2"
    assert ticket.service == "orders-api" and ticket.severity is Severity.medium


async def test_custom_jql_is_used_as_is(jira_app, worker, fake):
    await jira_app.state.db.set_setting("jira", {"enabled": True, "jql": "project = OPS AND labels = sre"})
    await worker.poll_due(force=True)
    assert fake.requests[0].url.params["jql"] == "project = OPS AND labels = sre"


# ------------------------------------------------------------------------ outbound

async def test_imported_issue_gets_pickup_comment_and_moves_to_in_progress(jira_app, worker, fake):
    db = jira_app.state.db
    fake.add_issue("KAN-7")
    (ticket_id,) = await worker.poll_due(force=True)
    await _send_all(worker, db, ticket_id)
    assert fake.issues["KAN-7"] == "In Progress"
    (key, body), = fake.comments
    assert key == "KAN-7" and ticket_id in body and f"http://192.168.1.20:8001/tickets/{ticket_id}" in body


async def test_full_lifecycle_comments_and_board_moves(jira_app, worker, fake):
    db = jira_app.state.db
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    await _send_all(worker, db, tid)
    last = (await db.list_events(tid))[-1].id

    for status, actor in ((TicketStatus.investigating, Actor.orchestrator), (TicketStatus.fix_proposed, Actor.orchestrator),
                          (TicketStatus.awaiting_approval, Actor.gate)):
        await db.set_status(tid, status, actor)
    await _send_all(worker, db, tid, after=last)
    assert fake.issues["KAN-7"] == "In Review"
    assert "Waiting for approval" in fake.comments[-1][1]
    last = (await db.list_events(tid))[-1].id

    await db.set_status(tid, TicketStatus.deploying, Actor.executor)
    await db.set_status(tid, TicketStatus.resolved, Actor.executor)
    await db.append_event(tid, TicketEventCreate(actor=Actor.executor, type=EventType.resolved, payload={
        "mttr": {"total_s": 82.0, "detection_s": 2.0, "diagnosis_s": 19.0}}))
    await _send_all(worker, db, tid, after=last)
    assert fake.issues["KAN-7"] == "Done"
    assert "Resolved by OCA in 1m 22s" in fake.comments[-1][1] and "diagnosis 19s" in fake.comments[-1][1]
    assert (await db.outbox_stats())["jira"]["failed"] == 0


async def test_failure_moves_issue_back_to_to_do(jira_app, worker, fake):
    db = jira_app.state.db
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    await db.set_status(tid, TicketStatus.failed, Actor.system)
    await _send_all(worker, db, tid)
    assert fake.issues["KAN-7"] == "To Do" and "needs a person" in fake.comments[-1][1]


async def test_detected_incident_opens_a_jira_issue_without_private_details(jira_app, worker, fake):
    db = jira_app.state.db
    ticket = await _health_ticket(db)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator)
    await _send_all(worker, db, ticket.id)

    create = fake.posted()[0]["fields"]
    assert create["project"] == {"key": "KAN"} and create["labels"] == ["oca-auto"]
    assert create["priority"] == {"name": "High"} and ticket.id in create["summary"]
    assert "SECRET" not in json.dumps(create) and "38" not in json.dumps(create)
    assert fake.issues["KAN-10"] == "In Progress"
    assert await db.get_link(ticket.id, "jira") == {"key": "KAN-10", "url": f"{URL}/browse/KAN-10"}
    assert (await db.get_ticket(ticket.id)).external_ref["key"] == "KAN-10"   # dashboard shows the Jira link


async def test_create_on_detect_can_be_switched_off(jira_app, worker, fake):
    db = jira_app.state.db
    await db.set_setting("jira", {"enabled": True, "project": "KAN", "create_on_detect": False})
    ticket = await _health_ticket(db)
    await db.set_status(ticket.id, TicketStatus.investigating, Actor.orchestrator)
    await _send_all(worker, db, ticket.id)
    assert fake.requests == []


async def test_create_retries_without_priority_when_project_lacks_it(jira_app, worker, fake):
    fake.fail_next.append(httpx.Response(400, json={"errors": {"priority": "Field 'priority' cannot be set."}}))
    ticket = await _health_ticket(jira_app.state.db)
    await _send_all(worker, jira_app.state.db, ticket.id)
    assert "priority" not in fake.posted()[-1]["fields"]
    assert await jira_app.state.db.get_link(ticket.id, "jira") is not None


async def test_unknown_status_name_fails_with_a_clear_message(jira_app, worker, fake):
    db = jira_app.state.db
    await db.set_setting("jira", {"enabled": True, "project": "KAN", "status_map": {"investigating": "Doing"}})
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    await _send_all(worker, db, tid)
    assert (await db.outbox_stats())["jira"]["failed"] == 1
    assert "no transition" in worker.state["jira"].last_error and "Doing" in worker.state["jira"].last_error


async def test_bad_credentials_fail_permanently(jira_app, worker, fake):
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    fake.fail_next.append(httpx.Response(401, json={"errorMessages": ["Unauthorized"]}))
    await _send_all(worker, jira_app.state.db, tid)
    db = jira_app.state.db
    assert (await db.outbox_stats())["jira"]["failed"] == 1
    (row,) = await db._all("SELECT last_error FROM integration_outbox WHERE status = 'failed'")
    assert "HTTP 401" in row["last_error"] and "OCA_JIRA_API_TOKEN" in row["last_error"]


@pytest.mark.parametrize("failure", [httpx.Response(503, text="down"), httpx.Response(429, json={}),
                                     httpx.ConnectError("no network")])
async def test_temporary_problems_retry_later(jira_app, worker, fake, failure):
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    fake.fail_next.append(failure)
    await _send_all(worker, jira_app.state.db, tid)
    stats = (await jira_app.state.db.outbox_stats())["jira"]
    assert stats["pending"] == 2 and stats["failed"] == 0   # the comment waits, and the move waits behind it


async def test_failed_poll_is_retried_next_interval(jira_app, worker, fake):
    fake.fail_next.append(httpx.ConnectError("no network"))
    assert await worker.poll_due(force=True) == []
    assert "cannot reach Jira" in worker.state["jira"].last_error


async def test_secrets_never_leave_in_bodies(jira_app, worker, fake):
    fake.add_issue("KAN-7")
    (tid,) = await worker.poll_due(force=True)
    await _send_all(worker, jira_app.state.db, tid)
    assert all(TOKEN not in json.dumps(b) and EMAIL not in json.dumps(b) for b in fake.posted())


async def test_needs_credentials_and_a_project(jira_app, settings):
    jira = JiraIntegration(jira_app)
    await jira_app.state.db.set_setting("jira", {"enabled": True})
    assert not jira.enabled(await jira.config())
    await jira_app.state.db.set_setting("jira", {"enabled": True, "project": "KAN"})
    assert jira.enabled(await jira.config())
    assert not JiraIntegration(create_app(settings)).has_credentials()


async def test_jira_is_registered(jira_app):
    assert [i.name for i in build_integrations(jira_app)] == ["jira", "slack"]


def test_adf_to_text():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "line one"}, {"type": "hardBreak"},
                                          {"type": "text", "text": "line two"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "next"}]}]}
    assert adf_to_text(doc) == "line one\nline two\nnext\n"
    assert adf_to_text("plain") == "plain" and adf_to_text(None) == ""
