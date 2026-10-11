import pytest

from oca.auth import SESSION_COOKIE
from oca.schemas import Role
from tests.test_actions_e2e import REVIEW_OK, detect, prove

pytestmark = pytest.mark.anyio


async def test_login_flow_and_pages(client, auth):
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/dash")).headers["location"] == "/login"
    assert (await client.get("/login")).status_code == 200
    bad = await client.post("/login", data={"username": "admin", "password": "nope"})
    assert bad.status_code == 200 and "Invalid" in bad.text
    good = await client.post("/login", data={"username": "admin", "password": "admin"})
    assert good.status_code == 303 and SESSION_COOKIE in good.cookies
    cookie = {"Cookie": f"{SESSION_COOKIE}={good.cookies[SESSION_COOKIE]}"}
    for path in ("/dash", "/tickets", "/deployments", "/health", "/logs", "/settings"):
        page = await client.get(path, headers=cookie)
        assert page.status_code == 200 and "<nav" in page.text, path
    fragment = await client.get("/tickets", headers={**cookie, "HX-Request": "true"})
    assert "<nav" not in fragment.text and 'id="ticket-table"' in fragment.text
    assert (await client.get("/static/htmx.min.js")).status_code == 200
    dash = await client.get("/dash", headers=cookie)
    assert "100% LOCAL" in dash.text and "INJECT INCIDENT" in dash.text


async def test_inject_detail_stream_and_approve_from_dashboard(app, client, auth):
    cookie = auth(Role.admin)
    inject = await client.post("/dash/inject", data={"incident": "timeout"}, headers=cookie)
    assert inject.status_code == 303 and inject.headers["location"] == "/tickets"
    ticket = (await app.state.db.list_tickets())[0]
    run = await prove(client, auth, ticket.id)
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=auth(Role.guardian))
    request = {"action": "set_config", "args": {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000},
               "ticket_id": ticket.id, "evidence": {"sandbox_run_id": run["id"]}}
    assert (await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))).json()["status"] == "awaiting_approval"

    detail = await client.get(f"/tickets/{ticket.id}", headers=cookie)
    assert detail.status_code == 200
    for marker in ("COMMAND GATE", "Sandbox before", "Sandbox after", ">Approve<", "sse-connect", "apply_patch"):
        assert marker in detail.text, marker
    stream = await client.get(f"/tickets/{ticket.id}/stream", params={"limit": 2, "timeout_s": 1}, headers=cookie)
    assert stream.headers["content-type"].startswith("text/event-stream") and "event: event" in stream.text and "<li" in stream.text
    gate = await client.get(f"/tickets/{ticket.id}/gate", headers=cookie)
    assert "awaiting approval" in gate.text

    approved = await client.post(f"/tickets/{ticket.id}/approve", data={"note": "from dashboard"}, headers=cookie)
    assert approved.status_code == 200 and 'id="gate-panel"' in approved.text
    dep_id = (await app.state.db.get_ticket(ticket.id)).current_deployment_id
    await app.state.executor.wait(dep_id)
    resolved = await client.get(f"/tickets/{ticket.id}", headers=cookie)
    assert "INCIDENT RESOLVED" in resolved.text and "MTTR" in resolved.text
    deployments = await client.get("/deployments", headers=cookie)
    assert "Restore Backup" in deployments.text and dep_id in deployments.text

    restored = await client.post(f"/deployments/{dep_id}/restore", headers=cookie)
    assert restored.status_code == 200 and "rolled back" in restored.text
    health = await client.post("/health/status_check/run", headers=cookie)
    assert "FAIL" in health.text  # restoring the backup brought the incident back


async def test_settings_form_roundtrip(app, client, auth):
    cookie = auth(Role.admin)
    saved = await client.post("/settings", data={"risk_threshold": "55", "cooldown_s": "0", "blocklist": "rollback, restart_service"}, headers=cookie)
    assert saved.status_code == 200 and "Saved." in saved.text
    assert await app.state.db.get_setting("require_human_approval") is False
    assert await app.state.db.get_setting("risk_threshold") == 55
    assert await app.state.db.get_setting("blocklist") == ["rollback", "restart_service"]


async def test_ticket_page_shows_engineer_diffs_and_commands(app, client, auth):
    cookie = auth(Role.admin)
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket = (await app.state.db.list_tickets())[0]
    assert (await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=auth(Role.orchestrator))).status_code == 200
    eng = auth(Role.engineer)
    assert (await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "DB_TIMEOUT_MS", "value": 3000},
                              headers=eng)).json()["applied"] is True
    assert (await client.post("/api/tools/sandbox/test", json={"ticket_id": ticket.id}, headers=eng)).json()["passed"] is True

    page = (await client.get(f"/tickets/{ticket.id}", headers=cookie)).text
    assert "Engineer's changes" in page and 'class="d-add">+DB_TIMEOUT_MS=3000' in page and 'class="d-del">-DB_TIMEOUT_MS=100' in page
    assert "$ git -C .sandbox apply" in page and "pytest services/orders-api/tests" in page and "test output" in page
    fragment = (await client.get(f"/tickets/{ticket.id}/changes", headers=cookie)).text
    assert 'id="changes"' in fragment and "set_config" in fragment and "command" in fragment


async def test_shutdown_button_stops_the_server(app, client, auth):
    import asyncio

    stopped = asyncio.Event()
    app.state.stopper = stopped.set
    assert (await client.get("/settings", headers=auth(Role.admin))).text.count('action="/shutdown"') == 2
    assert (await client.post("/api/shutdown", headers=auth(Role.engineer))).status_code == 403
    page = await client.post("/shutdown", headers=auth(Role.admin))
    assert page.status_code == 200 and "shutting down" in page.text
    assert (await client.post("/api/shutdown", headers=auth(Role.admin))).json()["status"] == "shutting_down"
    await asyncio.wait_for(stopped.wait(), 3)
