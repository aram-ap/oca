"""The sandbox judges by the production SLO set under production-matched load; a failed reproduction hands over
once; a deploy that improves production without clearing the SLO is held for a human instead of rolled back."""

from __future__ import annotations

import pytest

from oca.schemas import EventType, Role, SandboxMetrics, TicketStatus
from tests.test_actions_e2e import REVIEW_OK, detect, prove

pytestmark = pytest.mark.anyio


async def test_pool_saturation_alone_counts_as_reproduced(app):
    sandbox = app.state.sandbox
    thresholds = await sandbox._thresholds()
    assert thresholds["db_pool_utilization_pct"] == 90.0
    quiet = SandboxMetrics(requests=100, failures=0, error_rate_pct=0.0, p95_ms=200.0, db_pool_utilization_pct=100.0)
    assert sandbox._breaches(quiet, thresholds) == ["db_pool_utilization_pct"]
    unknown = SandboxMetrics(requests=100, failures=0, error_rate_pct=0.0, p95_ms=200.0)  # Prometheus had no series
    assert sandbox._breaches(unknown, thresholds) == []


async def test_sandbox_load_matches_production_rate(app, client, auth):
    ticket = await detect(app)  # the fake timeout incident reports 128 rps at detection
    n, rate = await app.state.sandbox._load(ticket.id)
    assert rate == 192 and n == 192 * 40  # 1.5x production, at least 40 s of load
    n2, _ = await app.state.sandbox._load(ticket.id, requests=9000)
    assert n2 == 9000
    prepared = (await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=auth(Role.orchestrator))).json()
    assert prepared["reproduced"] is True and prepared["before"]["rate"] == 192
    assert prepared["before"]["db_pool_utilization_pct"] == 62.0  # read alongside the HTTP numbers
    assert prepared["production"]["healthy"] is False and "error_rate_pct" in prepared["production"]["breaches"]
    event = [e for e in await app.state.db.list_events(ticket.id) if e.type is EventType.sandbox_result][-1]
    assert event.payload["load"] == {"requests": 7680, "rate": 192} and event.payload["production"]["healthy"] is False


async def _held_ticket(app, client, auth):
    await app.state.db.set_setting("require_human_approval", False)
    ticket = await detect(app)
    run = await prove(client, auth, ticket.id)
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=auth(Role.guardian))
    app.state.runner.failure_modes.add("deploy_partially_helps")
    request = {"action": "set_config", "args": {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000},
               "ticket_id": ticket.id, "evidence": {"sandbox_run_id": run["id"], "risk_score": 8, "guardian_decision": "APPROVE"}}
    decision = (await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))).json()
    assert decision["status"] == "policy_pass"
    dep_id = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()["current_deployment_id"]
    await app.state.executor.wait(dep_id)
    view = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert view["status"] == "awaiting_verification", view["status"]
    hold = [e for e in view["events"] if e["type"] == "run" and e["payload"]["stage"] == "hold"]
    assert len(hold) == 1 and "still breaches" in hold[0]["payload"]["reason"] and hold[0]["payload"]["pre_deploy"]["error_rate_pct"] > 20
    assert not any(e["type"] == "rolled_back" for e in view["events"])
    return ticket, dep_id


async def test_partial_fix_is_held_then_confirmed(app, client, auth):
    ticket, dep_id = await _held_ticket(app, client, auth)
    page = await client.get(f"/tickets/{ticket.id}", headers=auth(Role.admin))
    assert "Held for verification" in page.text and "Confirm resolved" in page.text and "Roll back" in page.text
    assert (await client.post(f"/api/tickets/{ticket.id}/confirm", headers=auth(Role.engineer))).status_code == 403
    confirmed = (await client.post(f"/api/tickets/{ticket.id}/confirm", json={"note": "load test was ours"}, headers=auth(Role.admin))).json()
    assert confirmed["status"] == "resolved" and confirmed["deployment_id"] == dep_id
    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "resolved" and final["events"][-1]["type"] in ("resolved", "report")
    decisions = [e["payload"] for e in final["events"] if e["type"] == "human_decision"]
    assert decisions[-1]["decision"] == "confirm" and decisions[-1]["note"] == "load test was ours"
    assert (await client.post(f"/api/tickets/{ticket.id}/confirm", headers=auth(Role.admin))).status_code == 409
    app.state.reports.delay_s = 0.0
    await app.state.reports.drain()
    report = app.state.reports.path_for(ticket.id).read_text()
    assert "held for human verification" in report and "confirmed the held deployment: load test was ours" in report


async def test_partial_fix_is_held_then_rolled_back_from_dashboard(app, client, auth):
    ticket, dep_id = await _held_ticket(app, client, auth)
    panel = await client.post(f"/tickets/{ticket.id}/rollback", data={"note": "not our load"}, headers=auth(Role.admin))
    assert panel.status_code == 200 and "rolled back" in panel.text
    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "rolled_back" and any(e["type"] == "rolled_back" for e in final["events"])
    deployments = {d["id"]: d for d in (await client.get("/api/deployments", headers=auth(Role.admin))).json()}
    assert deployments[dep_id]["status"] == "rolled_back"
    assert any(d["action"] == "rollback" and d["status"] == "succeeded" for d in deployments.values())


async def test_regression_is_still_rolled_back_automatically(app, client, auth):
    await app.state.db.set_setting("require_human_approval", False)
    ticket = await detect(app)
    run = await prove(client, auth, ticket.id)
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=auth(Role.guardian))
    app.state.runner.failure_modes.add("deploy_makes_worse")
    request = {"action": "deploy_patch", "args": {"ticket_id": ticket.id, "branch": f"fix/{ticket.id}"}, "ticket_id": ticket.id,
               "evidence": {"sandbox_run_id": run["id"]}}
    await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))
    dep_id = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()["current_deployment_id"]
    await app.state.executor.wait(dep_id)
    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "rolled_back"
    restore = [e for e in final["events"] if e["type"] == "run" and e["payload"]["stage"] == "auto_restore"][0]
    assert "worse than before the change" in restore["payload"]["reason"]


MIGRATION = {"name": "add_orders_customer_email_idx", "sql": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_customer_email ON orders (customer_email);"}


async def test_each_prepare_starts_from_a_clean_fix_branch_and_validate_reports_branch_changes(app, client, auth):
    runner, orch, eng = app.state.runner, auth(Role.orchestrator), auth(Role.engineer)
    ticket = await detect(app)
    assert (await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=orch)).status_code == 200
    stale = (await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "DB_POOL_MAX", "value": 300}, headers=eng)).json()
    assert stale["applied"] and f"fix/{ticket.id}" in runner.branches and runner.branches[f"fix/{ticket.id}"]["config"]["DB_POOL_MAX"] == 300

    # A new attempt drops the previous branch: its patches must not leak into the next fix.
    assert (await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=orch)).status_code == 200
    assert f"fix/{ticket.id}" not in runner.branches and f"git branch -D fix/{ticket.id}" in runner.calls
    migration = (await client.post("/api/tools/sandbox/migration", json={"ticket_id": ticket.id, **MIGRATION}, headers=eng)).json()
    assert migration["applied"] and migration["path"].endswith("002_add_orders_customer_email_idx.sql")
    config = (await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "DB_TIMEOUT_MS", "value": 3000}, headers=eng)).json()
    assert config["applied"] and runner.branches[f"fix/{ticket.id}"]["config"]["DB_POOL_MAX"] == 10  # baseline, not the stale 300
    assert (await client.post("/api/tools/sandbox/loadtest", json={"ticket_id": ticket.id}, headers=eng)).json()["phase"] == "after"
    validated = (await client.post(f"/api/sandbox/{ticket.id}/validate", headers=orch)).json()
    assert validated["passed"] is True
    changes = validated["changes"]
    assert changes["config_only"] is False and changes["migrations"] == ["services/orders-api/migrations/002_add_orders_customer_email_idx.sql"]
    assert changes["config_changes"] == {"DB_TIMEOUT_MS": "3000"} and len(changes["files"]) == 2
    event = [e for e in await app.state.db.list_events(ticket.id) if e.type is EventType.sandbox_result][-1]
    assert event.payload["changes"] == changes
