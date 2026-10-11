"""Autonomous by default and the dashboard time zone."""

from __future__ import annotations

import pytest

from oca.config import Settings
from oca.main import create_app, shutdown, startup
from oca.schemas import Role

pytestmark = pytest.mark.anyio


def test_human_approval_is_off_unless_asked_for(tmp_path):
    assert Settings(db_path=tmp_path / "a.db").require_human_approval is False
    assert Settings.from_env({}, dotenv=None).require_human_approval is False
    assert Settings.from_env({"OCA_REQUIRE_HUMAN_APPROVAL": "true"}, dotenv=None).require_human_approval is True
    with pytest.raises(ValueError):
        Settings(db_path=tmp_path / "a.db", timezone="Mars/Olympus")


async def test_gate_auto_approves_by_default(tmp_path):
    settings = Settings(db_path=tmp_path / "oca.db", data_dir=tmp_path / "d", runner="dry", autostart_pipeline=False,
                        check_interval_s=3600, stage_delay_s=0.0, verify_runs=1, verify_interval_s=0.0, loadtest_requests=50)
    app = create_app(settings)
    await startup(app)
    try:
        assert await app.state.db.get_setting("require_human_approval") is False
        assert await app.state.db.get_setting("timezone") == "UTC" and app.state.tz_name == "UTC"
    finally:
        await shutdown(app)


async def test_timezone_setting_changes_rendered_times(app, client, auth):
    admin = auth(Role.admin)
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket = (await app.state.db.list_tickets())[0]
    utc_page = (await client.get(f"/tickets/{ticket.id}", headers=admin)).text
    utc_label = ticket.created_at.strftime("%Y-%m-%d %H:%M:%S")
    assert utc_label in utc_page

    bad = await client.put("/api/settings", json={"timezone": "Nowhere/Town"}, headers=admin)
    assert bad.status_code == 422 and "IANA" in bad.json()["detail"]
    ok = await client.put("/api/settings", json={"timezone": "Asia/Tokyo"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["timezone"] == "Asia/Tokyo"
    tokyo_page = (await client.get(f"/tickets/{ticket.id}", headers=admin)).text
    from zoneinfo import ZoneInfo
    assert ticket.created_at.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S") in tokyo_page
    assert utc_label not in tokyo_page

    form = {"risk_threshold": "30", "cooldown_s": "120", "blocklist": "", "timezone": "America/Los_Angeles"}
    saved = await client.post("/settings", data=form, headers=admin)
    assert saved.status_code == 200 and "currently America/Los_Angeles" in saved.text
    assert await app.state.db.get_setting("timezone") == "America/Los_Angeles"
    saved = await client.post("/settings", data={**form, "timezone": "garbage"}, headers=admin)
    assert "currently America/Los_Angeles" in saved.text  # an invalid zone keeps the previous one
    # The SSE row renderer uses the same zone.
    events = await app.state.db.list_events(ticket.id)
    row = (await client.get(f"/tickets/{ticket.id}/timeline", headers=admin)).text
    assert events[0].ts.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M:%S") in row
