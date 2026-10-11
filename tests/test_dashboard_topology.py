"""Status-driven map checks: unknown or stale health must not look healthy."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from oca.schemas import Role
from oca.web.topology import topology_context


def check(ok=True, *, age=0, result=True):
    return SimpleNamespace(name="status_check", enabled=True, last_ok=ok,
                           last_result={"detail": "health"} if result else None,
                           last_run=datetime.now(timezone.utc) - timedelta(seconds=age), interval_s=10)


def context(checks, *, loaded=None, tickets=None, sandbox=None):
    return topology_context(checks, tickets or [], {"loaded": loaded, "model": "nemotron3:33b"},
                            {"sandbox": {"up": sandbox}} if sandbox is not None else None)


def test_unknown_stale_and_failed_health_have_distinct_signals():
    assert context([])["overall"] == "pending"
    assert context([check(result=False)])["overall"] == "pending"
    assert context([check(age=120)])["overall"] == "pending"
    healthy = context([check()], loaded=True)
    assert healthy["overall"] == "healthy"
    assert next(n for n in healthy["nodes"] if n["key"] == "production")["state"] == "healthy"
    failed = context([check(False)], loaded=True)
    assert failed["overall"] == "danger"
    assert any(w["state"] == "danger" for w in failed["wires"])


def test_model_idle_and_sandbox_unknown_do_not_claim_activity():
    nodes = {n["key"]: n for n in context([check()], loaded=False)["nodes"]}
    assert nodes["model"]["state"] == "idle"
    assert nodes["sandbox"]["state"] == "pending"
    assert all(nodes[role]["state"] == "idle" for role in ("investigator", "engineer", "guardian"))


def test_agent_activity_follows_recorded_ticket_stage():
    ticket = SimpleNamespace(status=SimpleNamespace(value="investigating"))
    result = context([check()], loaded=True, tickets=[ticket])
    nodes = {n["key"]: n for n in result["nodes"]}
    assert nodes["investigator"]["state"] == "active"
    assert nodes["engineer"]["state"] == "idle"
    assert nodes["guardian"]["state"] == "idle"
    assert nodes["orchestrator"]["state"] == "active"


@pytest.mark.anyio
async def test_map_fragment_auth_and_incident_refresh(app, client, auth):
    assert (await client.get("/dash/topology")).status_code == 303
    cookie = auth(Role.admin)
    healthy = await client.get("/dash/topology", headers=cookie)
    assert healthy.status_code == 200
    assert 'id="system-map"' in healthy.text and "<nav" not in healthy.text
    assert "SIMULATED INFRASTRUCTURE" in healthy.text
    await client.post("/dash/inject", data={"incident": "timeout"}, headers=cookie)
    broken = await client.get("/dash/topology", headers=cookie)
    assert broken.status_code == 200
    assert 'aria-label="Production: Needs attention"' in broken.text
    assert "state-danger" in broken.text
    dash = await client.get("/dash", headers=cookie)
    assert 'data-toggle-signals' in dash.text
    assert '/static/command-center.css' in dash.text
    for path in ("/static/command-center.css", "/static/dashboard.js"):
        assert (await client.get(path)).status_code == 200


def test_idle_agents_keep_idle_wires_while_a_ticket_is_open():
    """Only the agent in the current stage is wired live to the orchestrator; idle agents stay dark."""
    from types import SimpleNamespace
    ticket = SimpleNamespace(status=SimpleNamespace(value="investigating"))
    topo = context([], loaded=False, tickets=[ticket])
    nodes = {n["key"]: n for n in topo["nodes"]}
    assert nodes["orchestrator"]["state"] == "active" and nodes["investigator"]["state"] == "active"
    assert nodes["engineer"]["state"] == nodes["guardian"]["state"] == "idle"
    wires = {w["path"]: w["state"] for w in topo["wires"]}
    assert wires["M260 293 V320 H550 V345"] == "active"   # investigator -> orchestrator
    assert wires["M550 293 V345"] == "idle"               # engineer -> orchestrator
    assert wires["M840 293 V320 H550 V345"] == "idle"     # guardian -> orchestrator
    assert wires["M550 433 V465"] == "active"             # orchestrator -> api stays live


def test_guardian_is_active_while_the_review_runs():
    """The orchestrator sets `reviewed` before calling the Guardian, so that is the stage the review runs under."""
    for stage in ("sandbox_validated", "reviewed"):
        ticket = SimpleNamespace(status=SimpleNamespace(value=stage))
        nodes = {n["key"]: n for n in context([check()], loaded=True, tickets=[ticket])["nodes"]}
        assert nodes["guardian"]["state"] == "active" and nodes["guardian"]["label"] == "Reviewing", stage
        assert nodes["engineer"]["state"] == nodes["investigator"]["state"] == "idle", stage
    ticket = SimpleNamespace(status=SimpleNamespace(value="gate"))
    nodes = {n["key"]: n for n in context([check()], loaded=True, tickets=[ticket])["nodes"]}
    assert nodes["guardian"]["state"] == "idle"
