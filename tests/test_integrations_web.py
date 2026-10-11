"""Integrations API, Integrations page, Settings forms and the Local AI panel."""

import dataclasses
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from oca.auth import SESSION_COOKIE, make_session
from oca.main import create_app, shutdown, startup
from oca.schemas import Role

pytestmark = pytest.mark.anyio

SLACK_TOKEN = "xoxb-SECRET-slack"
JIRA_TOKEN = "ATATT-SECRET-jira"


@pytest.fixture
async def make_client(settings):
    """make_client(**settings_overrides) -> (app, client, admin cookie headers). Cleans up after the test."""
    opened = []

    async def build(**overrides):
        app = create_app(dataclasses.replace(settings, **overrides))
        await startup(app)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://oca.test")
        opened.append((app, client))
        cookie = {"Cookie": f"{SESSION_COOKIE}={make_session(app.state.settings, 'admin')}"}
        return app, client, cookie

    yield build
    for app, client in opened:
        await client.aclose()
        await shutdown(app)


def fake_transport(handler, seen):
    def record(request):
        seen.append(request)
        return handler(request)
    return httpx.MockTransport(record)


async def test_api_lists_integrations_switched_off_by_default(client, auth):
    data = (await client.get("/api/integrations", headers=auth(Role.admin))).json()
    assert [d["name"] for d in data] == ["jira", "slack"]
    assert all(not d["configured"] and d["outbound_detail"] == "off" for d in data)


async def test_connection_test_needs_admin_and_a_known_name(client, auth):
    assert (await client.post("/api/integrations/jira/test", headers=auth(Role.engineer))).status_code == 403
    assert (await client.post("/api/integrations/nope/test", headers=auth(Role.admin))).status_code == 404
    result = (await client.post("/api/integrations/jira/test", headers=auth(Role.admin))).json()
    assert not result["ok"] and "OCA_JIRA_URL" in result["detail"]


async def test_slack_test_button_posts_to_the_channel(make_client):
    app, client, cookie = await make_client(slack_bot_token=SLACK_TOKEN)
    await app.state.db.set_setting("slack", {"enabled": False, "channel_id": "C123"})
    seen = []
    app.state.integrations.integrations["slack"]._transport = fake_transport(
        lambda r: httpx.Response(200, json={"ok": True, "ts": "1.0", "channel": "C123"}), seen)

    result = (await client.post("/api/integrations/slack/test", headers=cookie)).json()
    assert result["ok"] and json.loads(seen[0].content)["channel"] == "C123"

    page = await client.post("/integrations/slack/test", headers=cookie)
    assert page.status_code == 200 and "Test message posted" in page.text and ">OK<" in page.text


async def test_slack_test_explains_a_missing_invite(make_client):
    app, client, cookie = await make_client(slack_bot_token=SLACK_TOKEN)
    await app.state.db.set_setting("slack", {"channel_id": "C123"})
    app.state.integrations.integrations["slack"]._transport = fake_transport(
        lambda r: httpx.Response(200, json={"ok": False, "error": "not_in_channel"}), [])
    result = (await client.post("/api/integrations/slack/test", headers=cookie)).json()
    assert not result["ok"] and "/invite" in result["detail"]


def jira_board(request):
    path = request.url.path
    if path == "/rest/api/2/myself":
        return httpx.Response(200, json={"displayName": "Nolan"})
    if path == "/rest/api/2/project/KAN":
        return httpx.Response(200, json={"key": "KAN", "name": "Hackathon.Dev"})
    if path == "/rest/api/2/project/KAN/statuses":
        return httpx.Response(200, json=[{"name": "Task", "statuses": [
            {"name": n} for n in ("To Do", "In Progress", "In Review", "Done")]}])
    return httpx.Response(404, json={})


async def test_jira_test_checks_login_project_and_board(make_client):
    app, client, cookie = await make_client(jira_url="https://team.atlassian.net", jira_email="me@example.com",
                                            jira_api_token=JIRA_TOKEN)
    await app.state.db.set_setting("jira", {"project": "KAN"})
    app.state.integrations.integrations["jira"]._transport = fake_transport(jira_board, [])
    result = (await client.post("/api/integrations/jira/test", headers=cookie)).json()
    assert result["ok"] and any("Nolan" in line for line in result["lines"])

    await app.state.db.set_setting("jira", {"project": "KAN", "status_map": {"investigating": "Doing"}})
    result = (await client.post("/api/integrations/jira/test", headers=cookie)).json()
    assert not result["ok"] and "ERR investigating -> 'Doing'" in result["lines"]


async def test_slack_settings_form_saves_and_shows_missing_token(app, client, auth):
    cookie = auth(Role.admin)
    page = await client.post("/settings/integrations/slack", headers=cookie,
                             data={"enabled": "on", "channel_id": " C123 ", "outbound_detail": "summary"})
    assert page.status_code == 200 and "Slack settings saved" in page.text and "OCA_SLACK_BOT_TOKEN" in page.text
    assert await app.state.db.get_setting("slack") == {"enabled": True, "channel_id": "C123",
                                                        "outbound_detail": "summary"}


async def test_jira_settings_form_saves_and_rejects_bad_status_map(app, client, auth):
    cookie = auth(Role.admin)
    await app.state.db.set_setting("jira", {"kept": "yes"})
    form = {"enabled": "on", "project": "kan", "jql": "", "poll_interval_s": "5", "default_service": "",
            "issue_type": "Task", "outbound_detail": "minimal",
            "status_map": "investigating=In Progress\nawaiting_approval=Review"}
    page = await client.post("/settings/integrations/jira", headers=cookie, data=form)
    assert "Jira settings saved" in page.text
    stored = await app.state.db.get_setting("jira")
    assert stored["project"] == "KAN" and stored["poll_interval_s"] == 10 and stored["create_on_detect"] is False
    assert stored["status_map"] == {"investigating": "In Progress", "awaiting_approval": "Review"}
    assert stored["default_service"] == "orders-api" and stored["kept"] == "yes"

    bad = await client.post("/settings/integrations/jira", headers=cookie, data={**form, "status_map": "nonsense"})
    assert "not saved" in bad.text and (await app.state.db.get_setting("jira"))["status_map"] == stored["status_map"]
    bad = await client.post("/settings/integrations/jira", headers=cookie, data={**form, "project": "KAN; DROP"})
    assert "not saved" in bad.text


async def test_unknown_integration_or_privacy_level_is_rejected(app, client, auth):
    cookie = auth(Role.admin)
    assert (await client.post("/settings/integrations/nope", headers=cookie, data={})).status_code == 404
    page = await client.post("/settings/integrations/slack", headers=cookie, data={"outbound_detail": "everything"})
    assert "not saved" in page.text and await app.state.db.get_setting("slack") == {}


async def test_credentials_never_appear_in_pages_or_api(make_client):
    app, client, cookie = await make_client(slack_bot_token=SLACK_TOKEN, jira_url="https://team.atlassian.net",
                                            jira_email="me@example.com", jira_api_token=JIRA_TOKEN)
    for path in ("/settings", "/integrations", "/dash", "/api/settings", "/api/integrations", "/api/stats/local-ai"):
        response = await client.get(path, headers=cookie)
        assert response.status_code == 200, path
        assert SLACK_TOKEN not in response.text and JIRA_TOKEN not in response.text, path


async def test_integrations_page_and_nav(app, client, auth):
    cookie = auth(Role.admin)
    page = await client.get("/integrations", headers=cookie)
    assert page.status_code == 200 and 'id="integration-jira"' in page.text and 'id="integration-slack"' in page.text
    assert 'href="/integrations"' in (await client.get("/dash", headers=cookie)).text


async def test_local_ai_panel_shows_what_leaves_the_machine(make_client):
    app, client, cookie = await make_client(slack_bot_token=SLACK_TOKEN)
    stats = (await client.get("/api/stats/local-ai", headers=cookie)).json()
    assert stats["ticketing_mode"].startswith("not connected")

    await app.state.db.set_setting("slack", {"enabled": True, "channel_id": "C123"})
    stats = (await client.get("/api/stats/local-ai", headers=cookie)).json()
    assert stats["ticketing_mode"].startswith("slack · metadata only")
    panel = await client.get("/dash/local-ai", headers=cookie)
    assert "Slack sync</dt><dd>Metadata only · 0 bytes" in panel.text and "Jira sync</dt><dd>Not connected" in panel.text
