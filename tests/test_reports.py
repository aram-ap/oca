"""Incident reports: written on every terminal status, rendered on the dashboard, linked from Jira/Slack."""

from __future__ import annotations

import httpx
import pytest

from oca.schemas import EventType, Role, TicketStatus
from tests.test_actions_e2e import REVIEW_OK, detect, prove
from tests.test_integrations import FakeJira, _configure

pytestmark = pytest.mark.anyio


async def _resolve(app, client, auth):
    ticket = await detect(app)
    run = await prove(client, auth, ticket.id)
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=auth(Role.guardian))
    request = {"action": "set_config", "args": {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000},
               "ticket_id": ticket.id, "evidence": {"sandbox_run_id": run["id"], "risk_score": 8, "guardian_decision": "APPROVE"}}
    assert (await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))).json()["status"] == "awaiting_approval"
    approved = (await client.post(f"/api/tickets/{ticket.id}/approve", json={"note": "demo"}, headers=auth(Role.admin))).json()
    await app.state.executor.wait(approved["deployment_id"])
    return ticket


async def test_resolved_ticket_gets_a_full_report(app, client, auth):
    app.state.reports.delay_s = 0.0
    ticket = await _resolve(app, client, auth)
    await app.state.reports.drain()

    events = await app.state.db.list_events(ticket.id)
    report_events = [e for e in events if e.type is EventType.report]
    assert len(report_events) == 1 and events[-1].type is EventType.report
    payload = report_events[0].payload
    assert payload["status"] == "resolved" and payload["url"].endswith(f"/tickets/{ticket.id}/report")
    assert payload["summary"] == f"{ticket.id} resolved for orders-api. Fix: apply_patch on services/orders-api/config/app.env."

    path = app.state.reports.path_for(ticket.id)
    assert path.exists() and path.parent == app.state.settings.data_dir / "reports"
    text = path.read_text()
    for marker in (f"# Incident report {ticket.id}", "## Summary", "## Impact", "## Timeline", "## Root cause", "## Fix",
                   "## Validation", "## Review and approval", "## Follow-ups", "+DB_TIMEOUT_MS=3000", "**MTTR:**",
                   "incident resolved", "Guardian decision: **APPROVE**", "Operator approved: demo", "backup taken"):
        assert marker in text, marker
    assert "get_code" not in text  # read-only tool calls stay on the timeline, not in the report

    api = await client.get(f"/api/tickets/{ticket.id}/report", headers=auth(Role.admin))
    assert api.status_code == 200 and api.headers["content-type"].startswith("text/markdown")
    assert api.text.startswith(f"# Incident report {ticket.id}") and "## Validation" in api.text
    page = await client.get(f"/tickets/{ticket.id}/report", headers=auth(Role.admin))
    assert page.status_code == 200 and f"<h1>Incident report {ticket.id}" in page.text and "<h2>Root cause</h2>" in page.text
    assert '<pre class="diff">' in page.text and "<table" in page.text and "Download Markdown" in page.text
    download = await client.get(f"/tickets/{ticket.id}/report.md", headers=auth(Role.admin))
    assert download.headers["content-disposition"].endswith('incident-report.md"')
    detail = await client.get(f"/tickets/{ticket.id}", headers=auth(Role.admin))
    assert f'href="/tickets/{ticket.id}/report"' in detail.text and "Incident report written" in detail.text


async def test_failed_ticket_gets_a_report_and_open_tickets_preview(app, client, auth):
    app.state.reports.delay_s = 0.0
    ticket = await detect(app)
    preview = await client.get(f"/tickets/{ticket.id}/report", headers=auth(Role.admin))
    assert preview.status_code == 200 and "live preview" in preview.text
    assert not app.state.reports.path_for(ticket.id).exists()

    await app.state.db.set_status(ticket.id, TicketStatus.failed, actor="orchestrator", note="attempts exhausted")
    await app.state.reports.drain()
    text = app.state.reports.path_for(ticket.id).read_text()
    assert "**Status:** failed" in text and "a human must take over" in text and "No fix was applied" in text
    assert "ticket failed: attempts exhausted" in text


async def test_report_link_is_posted_to_jira(app, client, auth):
    fake = FakeJira()
    app.state.integrations["jira"].transport = httpx.MockTransport(fake.handler)
    await _configure(client, auth, outbound_detail="summary")
    app.state.reports.delay_s = 0.0
    db, relay, outbox = app.state.db, app.state.relay, app.state.outbox

    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket = (await db.list_tickets())[0]
    await relay.drain()
    await outbox.pump()
    await db.set_status(ticket.id, TicketStatus.resolved, actor="executor")
    await app.state.reports.drain()
    await relay.drain()
    await outbox.pump()
    comment = fake.comments["SRE-101"][-1]
    assert "Incident report" in comment and f"/tickets/{ticket.id}/report" in comment and ticket.id in comment
    assert "DB_TIMEOUT_MS" not in comment  # no fix applied in this run; the summary is the outcome line only
