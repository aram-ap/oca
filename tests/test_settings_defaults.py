"""Autonomous by default, dashboard time zone, and the stale-sandbox guard in the real runner."""

from __future__ import annotations

import json

import pytest

from oca.config import Settings
from oca.fakeco.subprocess_runner import SubprocessRunner
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


@pytest.fixture
def stub_runner(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "deploy.sh").write_text("#!/bin/sh\n")
    r = SubprocessRunner(tmp_path, "http://prom:9090", "http://prom:19090", "http://api:18000", {})
    r.commands, r.state = [], {"branch": "main", "worktrees": set()}

    async def fake_run(*argv, cwd=None, timeout=120, env=None, stdin=None, check=True):
        r.commands.append(list(argv))
        if argv[:3] == ("git", "worktree", "list"):
            return 0, "".join(f"worktree {w}\nHEAD abc\n\n" for w in [str(tmp_path)] + sorted(r.state["worktrees"])), ""
        if argv[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return 0, r.state["branch"] + "\n", ""
        if argv[:2] == ("git", "checkout"):
            r.state["branch"] = argv[-1]
            return 0, "", ""
        if argv[:2] == ("ops/sandbox.sh", "up"):
            (tmp_path / ".sandbox").mkdir(exist_ok=True)
            r.state["worktrees"].add(str(tmp_path / ".sandbox"))
            return 0, json.dumps({"sandbox": "up", "git_sha": "abc1234567", "version": "1.3.1"}), ""
        return 0, "", ""
    r._run = fake_run
    return r


async def test_stale_sandbox_dir_is_moved_aside_and_production_returned_to_main(stub_runner, tmp_path):
    stale = tmp_path / ".sandbox"
    (stale / "logs-sandbox").mkdir(parents=True)
    (stale / "sandbox.env").write_text("x")
    stub_runner.state["branch"] = "fix/INC-0001"  # left behind by the misdirected checkout
    info = await stub_runner.sandbox_up("abc1234567")
    assert info.git_sha == "abc1234567"
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".sandbox-stale-")]
    assert len(leftovers) == 1 and (leftovers[0] / "sandbox.env").exists()
    assert ["git", "worktree", "prune"] in stub_runner.commands
    assert ["git", "checkout", "-q", "main"] in stub_runner.commands and stub_runner.state["branch"] == "main"
    assert stub_runner.commands.index(["git", "checkout", "-q", "main"]) < stub_runner.commands.index(["ops/sandbox.sh", "up", "abc1234567"])

    # A registered worktree is left alone on the next run.
    stub_runner.commands.clear()
    await stub_runner.sandbox_up("abc1234567")
    assert not [c for c in stub_runner.commands if c[:3] == ["git", "worktree", "prune"]]
    assert len([p for p in tmp_path.iterdir() if p.name.startswith(".sandbox-stale-")]) == 1
