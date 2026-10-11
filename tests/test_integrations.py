"""Jira/Slack integrations: outbound policy, outbox delivery and retries, inbound polling, secret handling."""

from __future__ import annotations

import json

import httpx
import pytest

from oca.integrations.base import MASK
from oca.schemas import EventType, OutboxStatus, Role, TicketEventCreate, TicketSource, TicketStatus

pytestmark = pytest.mark.anyio

JIRA = {"enabled": True, "base_url": "https://acme.atlassian.net", "email": "oncall@acme.test", "token": "secret-token",
        "project": "SRE", "issue_type": "Task", "outbound_detail": "minimal", "create_on_detect": True}


class FakeJira:
    """Just enough of Jira Cloud's REST API for the integration, as an httpx MockTransport."""

    def __init__(self, fail_with: int | None = None):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.search_results: list[dict] = []
        self.fail_with = fail_with
        self.next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        assert request.headers["Authorization"].startswith("Basic ")
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"errorMessages": ["boom"]})
        if path == "/rest/api/2/myself":
            return httpx.Response(200, json={"displayName": "On Call", "emailAddress": "oncall@acme.test"})
        if path == "/rest/api/2/project/SRE":
            return httpx.Response(200, json={"name": "Site Reliability", "issueTypes": [{"name": "Task"}, {"name": "Bug"}]})
        if path == "/rest/api/2/project/SRE/statuses":
            return httpx.Response(200, json=[{"name": "Task", "statuses": [{"name": "To Do"}, {"name": "In Progress"}, {"name": "Done"}]}])
        if path == "/rest/api/2/issue" and request.method == "POST":
            self.next_id += 1
            key = f"SRE-{self.next_id}"
            self.issues[key] = {"status": "To Do", "labels": list(body["fields"].get("labels", [])), "fields": body["fields"]}
            return httpx.Response(201, json={"id": str(self.next_id), "key": key})
        if path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": self.search_results})
        if path.startswith("/rest/api/2/issue/"):
            key = path.split("/")[5]
            if key not in self.issues:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            issue = self.issues[key]
            if path.endswith("/comment"):
                self.comments.setdefault(key, []).append(body["body"])
                return httpx.Response(201, json={"id": "1"})
            if path.endswith("/transitions"):
                if request.method == "GET":
                    return httpx.Response(200, json={"transitions": [
                        {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                        {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
                        {"id": "31", "name": "Done", "to": {"name": "Done"}}]})
                issue["status"] = {"11": "To Do", "21": "In Progress", "31": "Done"}[body["transition"]["id"]]
                return httpx.Response(204)
            if request.method == "PUT":
                for op in body["update"]["labels"]:
                    if "add" in op and op["add"] not in issue["labels"]:
                        issue["labels"].append(op["add"])
                    if "remove" in op and op["remove"] in issue["labels"]:
                        issue["labels"].remove(op["remove"])
                return httpx.Response(204)
            fields = {"status": {"name": issue["status"]}, "labels": issue["labels"], **issue["fields"]}
            return httpx.Response(200, json={"key": key, "fields": fields})
        return httpx.Response(404, json={"errorMessages": [f"unhandled {path}"]})


@pytest.fixture
def fake_jira(app):
    fake = FakeJira()
    app.state.integrations["jira"].transport = httpx.MockTransport(fake.handler)
    return fake


async def _configure(client, auth, **overrides):
    response = await client.put("/api/settings", json={"jira": {**JIRA, **overrides}}, headers=auth(Role.admin))
    assert response.status_code == 200, response.text
    return response.json()


async def _open_incident(app):
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket = (await app.state.db.list_tickets())[0]
    await app.state.relay.drain()
    return ticket


async def test_outbound_creates_issue_then_follows_the_ticket(app, client, auth, fake_jira):
    await _configure(client, auth)
    db, relay, outbox = app.state.db, app.state.relay, app.state.outbox
    ticket = await _open_incident(app)
    assert await outbox.pump() == 1
    linked = await db.get_ticket(ticket.id)
    assert linked.external_ref == {"system": "jira", "key": "SRE-101", "url": "https://acme.atlassian.net/browse/SRE-101"}
    created = fake_jira.issues["SRE-101"]["fields"]
    assert created["summary"].startswith(f"[{ticket.id}]") and created["priority"] == {"name": "Highest"}
    assert "oca" in created["labels"] and "oca-detected" in created["labels"]
    assert "http://localhost:8001/tickets/" + ticket.id in created["description"]

    # A status change moves the issue and a diagnosis comments with the confidence only (minimal).
    await db.set_status(ticket.id, TicketStatus.investigating, actor="orchestrator")
    await db.append_event(ticket.id, TicketEventCreate(
        actor="investigator", type=EventType.diagnosis,
        payload={"diagnosis": {"diagnosis": "DB_TIMEOUT_MS was lowered to 100 ms; bestsellers_30d times out", "confidence": 0.9,
                               "evidence_refs": [], "unresolved_questions": []}}))
    await relay.drain()
    assert await outbox.pump() == 2
    assert fake_jira.issues["SRE-101"]["status"] == "In Progress"
    assert "oca-investigating" in fake_jira.issues["SRE-101"]["labels"] and "oca-detected" not in fake_jira.issues["SRE-101"]["labels"]
    comment = fake_jira.comments["SRE-101"][-1]
    assert "confidence 90%" in comment and "DB_TIMEOUT_MS" not in comment  # no agent free text at minimal

    # Resolution: Done plus an MTTR line; the timeline records every delivery with its byte count.
    await db.set_status(ticket.id, TicketStatus.resolved, actor="executor")
    await relay.drain()
    await outbox.pump()
    assert fake_jira.issues["SRE-101"]["status"] == "Done" and "MTTR" in fake_jira.comments["SRE-101"][-1]
    syncs = [e for e in await db.list_events(ticket.id) if e.type is EventType.external_sync]
    assert [s.payload["kind"] for s in syncs] == ["create_issue", "transition", "comment", "transition", "comment"]
    assert all(s.payload["bytes"] > 0 and s.payload["target"] == "jira" for s in syncs)
    stats = await db.outbox_stats()
    assert stats["jira"]["pending"] == 0 and stats["jira"]["failed"] == 0 and stats["jira"]["bytes_sent"] > 0
    page = await client.get(f"/tickets/{ticket.id}", headers=auth(Role.admin))
    assert "browse/SRE-101" in page.text


async def test_summary_level_adds_the_root_cause_one_liner(app, client, auth, fake_jira):
    await _configure(client, auth, outbound_detail="summary")
    ticket = await _open_incident(app)
    await app.state.outbox.pump()
    await app.state.db.append_event(ticket.id, TicketEventCreate(actor="investigator", type=EventType.diagnosis, payload={
        "diagnosis": {"diagnosis": "Line one.\n\nLine two " + "x" * 400, "confidence": 0.5, "evidence_refs": [], "unresolved_questions": []}}))
    await app.state.relay.drain()
    await app.state.outbox.pump()
    comment = fake_jira.comments["SRE-101"][-1]
    assert "Line one. Line two" in comment and "\n" not in comment.split("*[OCA]* ")[1].split(" Details")[0]
    assert len(comment) < 450  # bounded one-liner


async def test_off_and_disabled_send_nothing(app, client, auth, fake_jira):
    await _configure(client, auth, outbound_detail="off")
    await _open_incident(app)
    assert await app.state.db.list_outbox() == []
    await _configure(client, auth, enabled=False)
    await app.state.db.set_status((await app.state.db.list_tickets())[0].id, TicketStatus.investigating, actor="orchestrator")
    await app.state.relay.drain()
    assert await app.state.db.list_outbox() == [] and fake_jira.requests == []


async def test_tickets_without_an_issue_are_left_alone(app, client, auth, fake_jira):
    ticket = await _open_incident(app)  # Jira not connected yet: no issue, no outbox row
    await _configure(client, auth)
    await app.state.db.set_status(ticket.id, TicketStatus.investigating, actor="orchestrator")
    await app.state.relay.drain()
    assert await app.state.db.list_outbox() == [] and fake_jira.requests == []


async def test_delivery_retries_with_backoff_then_fails(app, client, auth):
    await _configure(client, auth)
    broken = FakeJira(fail_with=503)
    app.state.integrations["jira"].transport = httpx.MockTransport(broken.handler)
    app.state.outbox.max_attempts = 2
    db = app.state.db
    ticket = await _open_incident(app)
    assert await app.state.outbox.pump() == 0
    row = (await db.list_outbox())[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "HTTP 503" in row["last_error"]
    assert await app.state.outbox.pump() == 0  # not due yet: backoff
    await db.mark_outbox(row["id"], OutboxStatus.pending, row["attempts"], retry_in_s=0)
    assert await app.state.outbox.pump() == 0
    row = (await db.list_outbox())[0]
    assert row["status"] == "failed" and row["attempts"] == 2
    assert (await db.get_ticket(ticket.id)).external_ref is None
    status = (await client.get("/api/integrations", headers=auth(Role.admin))).json()
    assert status["jira"]["outbox"]["failed"] == 1 and status["jira"]["config"]["token"] == MASK


async def test_comment_waits_for_the_issue_to_exist(app, client, auth, fake_jira):
    await _configure(client, auth)
    db = app.state.db
    ticket = await _open_incident(app)
    await db.set_status(ticket.id, TicketStatus.investigating, actor="orchestrator")
    await app.state.relay.drain()
    rows = await db.list_outbox()
    assert [r["kind"] for r in reversed(rows)] == ["create_issue", "transition"]
    # Deliver the transition first: it defers (no attempt spent) because the create is still pending.
    transition = next(i for i in await db.due_outbox() if i["kind"] == "transition")
    assert not await app.state.outbox._deliver(transition)
    deferred = next(r for r in await db.list_outbox() if r["id"] == transition["id"])
    assert deferred["status"] == "pending" and deferred["attempts"] == 0
    await app.state.outbox.pump()  # creates the issue; the transition is scheduled a few seconds out
    await db.mark_outbox(transition["id"], OutboxStatus.pending, 0, retry_in_s=0)
    assert await app.state.outbox.pump() == 1
    assert fake_jira.issues["SRE-101"]["status"] == "In Progress"


async def test_inbound_poll_opens_each_issue_once_and_links_back(app, client, auth, fake_jira):
    await _configure(client, auth, jql="project = SRE AND labels = oca", create_on_detect=False)
    checks = {c["name"]: c for c in (await client.get("/api/checks", headers=auth(Role.admin))).json()}
    assert checks["jira_ticket_reader"]["enabled"] is True
    fake_jira.issues["SRE-7"] = {"status": "To Do", "labels": ["oca"], "fields": {
        "summary": "Checkout is slow for some customers", "description": "Reported by support: carts time out.",
        "priority": {"name": "High"}, "components": [{"name": "orders-api"}]}}
    fake_jira.search_results = [{"key": "SRE-7", "fields": {"summary": "Checkout is slow for some customers"}}]
    result = await app.state.scheduler.run_check("jira_ticket_reader")
    assert result.ok and "SRE-7" in result.detail
    tickets = await app.state.db.list_tickets()
    assert len(tickets) == 1 and tickets[0].source is TicketSource.jira and tickets[0].severity.value == "high"
    assert tickets[0].external_ref["key"] == "SRE-7" and tickets[0].description.startswith("Reported by support")
    events = await app.state.db.list_events(tickets[0].id)
    assert events[0].type is EventType.reported and events[0].actor.value == "jira"
    assert "no new issues" in (await app.state.scheduler.run_check("jira_ticket_reader")).detail
    assert len(await app.state.db.list_tickets()) == 1
    await app.state.relay.drain()
    await app.state.outbox.pump()
    assert any("is investigating" in c for c in fake_jira.comments["SRE-7"])  # link back, no duplicate issue
    assert not any(m == "POST" and p == "/rest/api/2/issue" for m, p, _ in fake_jira.requests)


async def test_connection_test_and_secret_handling(app, client, auth, fake_jira):
    await _configure(client, auth, jql="project = SRE")
    result = (await client.post("/api/integrations/jira/test", headers=auth(Role.admin))).json()
    assert result["ok"] and result["user"] == "On Call" and result["project"] == "Site Reliability"
    assert result["statuses"] == ["To Do", "In Progress", "Done"] and result["unmapped_statuses"] == []
    assert result["jql_matches"] == 0
    assert (await client.post("/api/integrations/jira/test", headers=auth(Role.engineer))).status_code == 403

    # GET never reveals the token; PUT with the mask (or blank) keeps the stored one.
    view = (await client.get("/api/settings", headers=auth(Role.admin))).json()
    assert view["jira"]["token"] == MASK and view["jira"]["project"] == "SRE"
    await client.put("/api/settings", json={"jira": {**JIRA, "token": MASK, "project": "OPS"}}, headers=auth(Role.admin))
    stored = await app.state.db.get_setting("jira")
    assert stored["token"] == "secret-token" and stored["project"] == "OPS"
    page = await client.get("/settings", headers=auth(Role.admin))
    assert "secret-token" not in page.text and "acme.atlassian.net" in page.text and "Integrations" in page.text

    # The settings form saves the same row and a bad credential reports a readable failure.
    form = {"enabled": "on", "base_url": "https://acme.atlassian.net/", "email": "oncall@acme.test", "token": "",
            "project": "sre", "issue_type": "Task", "deployment": "cloud", "outbound_detail": "summary",
            "create_on_detect": "on", "status_map": "awaiting_approval=In Review\nresolved=Done", "jql": "", "poll_interval_s": "45"}
    saved = await client.post("/settings/integrations/jira", data=form, headers=auth(Role.admin))
    assert saved.status_code == 303
    stored = await app.state.db.get_setting("jira")
    assert stored["token"] == "secret-token" and stored["project"] == "SRE" and stored["outbound_detail"] == "summary"
    assert stored["status_map"] == {"awaiting_approval": "In Review", "resolved": "Done"} and stored["poll_interval_s"] == 45
    fake_jira.fail_with = 401
    fragment = await client.post("/settings/integrations/jira/test", headers=auth(Role.admin))
    assert "Connection failed" in fragment.text and "HTTP 401" in fragment.text


async def test_slack_webhook_messages(app, client, auth):
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    app.state.integrations["slack"].transport = httpx.MockTransport(handler)
    response = await client.put("/api/integrations/slack", json={"enabled": True, "webhook_url": "https://hooks.slack.com/services/T/B/x"},
                                headers=auth(Role.admin))
    assert response.status_code == 200 and response.json()["webhook_url"] == MASK
    ticket = await _open_incident(app)
    await app.state.outbox.pump()
    await app.state.db.set_status(ticket.id, TicketStatus.awaiting_approval, actor="gate")
    await app.state.relay.drain()
    await app.state.outbox.pump()
    assert len(posts) == 2 and ticket.id in posts[0]["text"] and "Awaiting approval" in posts[1]["text"]
    assert (await client.post("/api/integrations/slack/test", headers=auth(Role.admin))).json()["ok"] is True
    assert "connection test" in posts[-1]["text"]
    stats = (await client.get("/api/stats/local-ai", headers=auth(Role.admin))).json()
    assert stats["ticketing_mode"].startswith("Slack") and stats["ticketing_bytes_sent"] > 0


async def test_env_seeds_settings_once(tmp_path):
    from oca.config import Settings
    from oca.main import create_app, shutdown, startup
    settings = Settings(db_path=tmp_path / "oca.db", data_dir=tmp_path / "d", runner="dry", autostart_pipeline=False,
                        check_interval_s=3600, jira_url="https://seed.atlassian.net", jira_email="a@b.c", jira_token="t",
                        jira_project="SEED", public_url="http://gx10.local:8001/")
    application = create_app(settings)
    await startup(application)
    try:
        stored = await application.state.db.get_setting("jira")
        assert stored["base_url"] == "https://seed.atlassian.net" and stored["enabled"] is True and stored["project"] == "SEED"
        await application.state.db.set_setting("jira", {**stored, "project": "EDITED"})
    finally:
        await shutdown(application)
    application = create_app(settings)
    await startup(application)
    try:
        assert (await application.state.db.get_setting("jira"))["project"] == "EDITED"  # the dashboard edit wins
        assert application.state.integrations["jira"].link("INC-1") == "http://gx10.local:8001/tickets/INC-1"
    finally:
        await shutdown(application)
