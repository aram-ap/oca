"""Inject -> ticket -> sandbox proof -> gate -> approve -> staged deploy -> resolved, on the FakeRunner."""

import pytest

from oca.schemas import Role, TicketStatus
from tests.test_fake_runner import INDEX_FIX, REVERT

pytestmark = pytest.mark.anyio

REVIEW_OK = {"risk_score": 8, "policy_concerns": [], "decision": "APPROVE",
             "justification": "One-line config revert, sandbox reproduced and fixed", "evidence_refs": ["sandbox:SBX-0001"]}


async def detect(app):
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    return (await app.state.db.list_tickets())[0]


async def prove(client, auth, ticket_id, diff=REVERT):
    orch, eng = auth(Role.orchestrator), auth(Role.engineer)
    prepared = (await client.post(f"/api/sandbox/{ticket_id}/prepare", headers=orch)).json()
    assert prepared["reproduced"] is True and prepared["before"]["error_rate_pct"] > 20
    assert (await client.post("/api/sandbox/validate".replace("sandbox/", f"sandbox/{ticket_id}/"), headers=orch)).status_code == 409
    patched = (await client.post("/api/tools/sandbox/patch", json={"ticket_id": ticket_id, "diff": diff}, headers=eng)).json()
    assert patched["applied"] and patched["patch_ref"] == f"fix/{ticket_id}"
    loadtest = (await client.post("/api/tools/sandbox/loadtest", json={"ticket_id": ticket_id, "requests": 200}, headers=eng)).json()
    assert loadtest["phase"] == "after" and loadtest["incident_present"] is False
    tests = (await client.post("/api/tools/sandbox/test", json={"ticket_id": ticket_id}, headers=eng)).json()
    assert tests["passed"] is True
    validated = (await client.post(f"/api/sandbox/{ticket_id}/validate", headers=orch)).json()
    assert validated["passed"] is True and validated["after"]["error_rate_pct"] == 0
    return validated


async def test_full_pipeline_with_human_approval(app, client, auth):
    ticket = await detect(app)
    orch, guard, admin = auth(Role.orchestrator), auth(Role.guardian), auth(Role.admin)
    run = await prove(client, auth, ticket.id)

    # Guardian tools see server-measured evidence and deterministic risk flags.
    diff = (await client.get("/api/tools/diff", params={"ticket_id": ticket.id}, headers=guard)).json()
    assert diff["risk_flags"]["config_only"] and "+DB_TIMEOUT_MS=3000" in diff["diff"]
    assert (await client.get("/api/tools/diff", params={"ticket_id": ticket.id}, headers=auth(Role.engineer))).status_code == 403
    results = (await client.get("/api/tools/sandbox-results", params={"ticket_id": ticket.id}, headers=guard)).json()
    assert results["reproduced"] and results["passed"]
    assert (await client.get("/api/tools/test-results", params={"ticket_id": ticket.id}, headers=guard)).json()["tests"]["passed"]

    # Without a Guardian review the gate denies at check 6.
    request = {"action": "set_config", "args": {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000},
               "ticket_id": ticket.id, "evidence": {"sandbox_run_id": run["id"], "risk_score": 8, "guardian_decision": "APPROVE"}}
    denied = (await client.post("/api/actions", json=request, headers=orch)).json()
    assert denied["status"] == "denied" and "guardian_approved" in denied["reason"]
    assert (await client.get(f"/api/tickets/{ticket.id}", headers=admin)).json()["status"] == "denied"

    # Guardian posts its review; the orchestrator resubmits; the gate passes and waits for a human.
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=guard)
    decision = (await client.post("/api/actions", json=request, headers=orch)).json()
    assert decision["status"] == "awaiting_approval" and all(c["ok"] for c in decision["checks"])
    assert (await client.post("/api/actions", json=request, headers=orch)).status_code == 409  # already pending
    assert (await client.post(f"/api/tickets/{ticket.id}/approve", headers=orch)).status_code == 403

    approved = (await client.post(f"/api/tickets/{ticket.id}/approve", json={"note": "demo"}, headers=admin)).json()
    await app.state.executor.wait(approved["deployment_id"])

    final = (await client.get(f"/api/tickets/{ticket.id}", headers=admin)).json()
    assert final["status"] == "resolved"
    types = [e["type"] for e in final["events"]]
    stages = [e["payload"]["stage"] for e in final["events"] if e["type"] == "run"]
    assert stages[:6] == ["snapshot", "canary", "monitor", "widen", "monitor", "full"] and "verify" in stages
    assert types[-1] == "resolved" and final["events"][-1]["payload"]["mttr"]["total_s"] is not None
    deployments = (await client.get("/api/deployments", headers=admin)).json()
    assert deployments[0]["status"] == "succeeded" and deployments[0]["backup_ref"] == f"backup/{deployments[0]['id']}"
    assert (await app.state.runner.status()).healthy and app.state.runner.config["DB_TIMEOUT_MS"] == 3000
    assert app.state.runner.sandbox["up"] is False  # torn down after resolve

    # Restore from the dashboard brings the incident back and is itself a rollback deployment.
    restored = (await client.post(f"/api/deployments/{deployments[0]['id']}/restore", headers=admin)).json()
    assert restored["action"] == "rollback" and restored["status"] == "succeeded"
    assert not (await app.state.runner.status()).healthy
    assert (await client.get(f"/api/deployments/{deployments[0]['id']}", headers=admin)).json()["status"] == "rolled_back"


async def test_autonomous_mode_and_auto_restore(app, client, auth):
    await app.state.db.set_setting("require_human_approval", False)
    ticket = await detect(app)
    run = await prove(client, auth, ticket.id)
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": REVIEW_OK}}, headers=auth(Role.guardian))
    app.state.runner.failure_modes.add("deploy_makes_worse")
    request = {"action": "deploy_patch", "args": {"ticket_id": ticket.id, "branch": f"fix/{ticket.id}"}, "ticket_id": ticket.id,
               "evidence": {"sandbox_run_id": run["id"]}}
    decision = (await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))).json()
    assert decision["status"] == "policy_pass" and decision["requires_human"] is False
    ticket_view = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    dep_id = ticket_view["current_deployment_id"]
    await app.state.executor.wait(dep_id)
    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "rolled_back"
    assert any(e["type"] == "rolled_back" for e in final["events"])
    deployments = {d["id"]: d for d in (await client.get("/api/deployments", headers=auth(Role.admin))).json()}
    assert deployments[dep_id]["status"] == "rolled_back"
    assert any(d["action"] == "rollback" and d["status"] == "succeeded" for d in deployments.values())
    assert (await app.state.runner.status()).error_rate_pct < 60  # back to the (still broken) pre-deploy state


async def test_gate_denies_high_risk_migration_and_human_override(app, client, auth):
    app.state.runner.inject("loyalty")
    await app.state.scheduler.tick(force=True)
    ticket = (await app.state.db.list_tickets())[0]
    run = await prove(client, auth, ticket.id, diff=INDEX_FIX)
    review = dict(REVIEW_OK, risk_score=45, policy_concerns=["DB migration: YES"])
    await client.post(f"/api/tickets/{ticket.id}/events", json={"type": "review", "payload": {"review": review}}, headers=auth(Role.guardian))
    request = {"action": "deploy_patch", "args": {"ticket_id": ticket.id, "branch": f"fix/{ticket.id}"}, "ticket_id": ticket.id,
               "evidence": {"sandbox_run_id": run["id"]}}
    decision = (await client.post("/api/actions", json=request, headers=auth(Role.orchestrator))).json()
    assert decision["status"] == "denied" and "risk 45/100" in decision["reason"]
    assert (await client.post(f"/api/tickets/{ticket.id}/deny", headers=auth(Role.admin))).status_code == 409
    override = (await client.post(f"/api/tickets/{ticket.id}/approve", json={"note": "index fix reviewed by DBA"}, headers=auth(Role.admin))).json()
    assert override["override"] is True
    await app.state.executor.wait(override["deployment_id"])
    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "resolved" and (await app.state.runner.status()).healthy


async def test_engineer_tools_require_prepare_and_reject_bad_diffs(app, client, auth):
    ticket = await detect(app)
    eng = auth(Role.engineer)
    assert (await client.post("/api/tools/sandbox/test", json={"ticket_id": ticket.id}, headers=eng)).status_code == 409
    await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=auth(Role.orchestrator))
    rejected = (await client.post("/api/tools/sandbox/patch", json={"ticket_id": ticket.id, "diff": "garbage"}, headers=eng)).json()
    assert rejected["applied"] is False and "git apply" in rejected["error"]
    assert (await client.post("/api/tools/sandbox/patch", json={"ticket_id": ticket.id, "diff": REVERT}, headers=auth(Role.investigator))).status_code == 403


async def test_restart_resumes_a_deployment_left_verifying(app, client, auth):
    """A deployment that reached `full` before the process died is re-verified and resolved on startup."""
    from oca.executor import start_executor_resume
    from oca.schemas import ActionName, Actor, DeploymentStatus, EventType, TicketEventCreate

    ticket = await detect(app)
    db = app.state.db
    # Apply the fix the way the executor would have before dying in the settle stage.
    record = await app.state.runner.set_config_and_deploy("DB_TIMEOUT_MS", 3000, ticket.id)
    pre = {"error_rate_pct": 25.0, "p95_latency_ms": 10000.0}
    dep = await db.create_deployment(ticket.id, ActionName.set_config, {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000},
                                     evidence={"pre_deploy": pre}, status=DeploymentStatus.running)
    await db.update_deployment(dep.id, git_sha=record.git_sha)
    await db.set_status(ticket.id, TicketStatus.deploying, Actor.executor, deployment_id=dep.id)
    for stage, pct in (("canary", 10), ("full", 100), ("settle", 100)):
        await db.append_event(ticket.id, TicketEventCreate(actor=Actor.executor, type=EventType.run,
                                                           payload={"stage": stage, "pct": pct, "deployment_id": dep.id}))
    # A second deployment that died mid-canary cannot be trusted and is failed for a human.
    other = await db.create_deployment(None, ActionName.restart_service, {"service": "orders-api"}, status=DeploymentStatus.running)

    await start_executor_resume(app)
    await app.state.executor.wait(dep.id)

    final = (await client.get(f"/api/tickets/{ticket.id}", headers=auth(Role.admin))).json()
    assert final["status"] == "resolved"
    stages = [e["payload"]["stage"] for e in final["events"] if e["type"] == "run"]
    assert "resume" in stages and "verify" in stages
    assert (await db.get_deployment(dep.id)).status is DeploymentStatus.succeeded
    assert (await db.get_deployment(other.id)).status is DeploymentStatus.failed
    # Idempotent: a second startup finds nothing to resume.
    assert await app.state.executor.resume_interrupted() == []
