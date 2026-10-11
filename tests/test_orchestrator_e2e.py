"""The real orchestrator and role agents, scripted model replies, over the ASGI app and FakeRunner."""

import json

import pytest
from httpx import AsyncClient

from models.agents import Engineer, Guardian, Investigator
from models.orchestrator import Orchestrator
from oca.schemas import Role
from tests.test_fake_runner import REVERT

pytestmark = pytest.mark.anyio


class AsgiClient:
    """Same surface as ToolAPIClient/PlatformClient but routed through httpx's ASGI transport."""

    def __init__(self, role: str, headers: dict, http: AsyncClient):
        self.role, self.headers, self.http = role, headers, http

    async def request(self, method, path, arguments=None):
        from models.tools.client import ToolAPIError

        if method == "GET":
            response = await self.http.get(path, params=arguments or {}, headers=self.headers)
        else:
            response = await self.http.post(path, json=arguments or {}, headers=self.headers)
        if response.status_code >= 400:
            raise ToolAPIError(f"HTTP {response.status_code}", response.status_code, response.text)
        return response.json() if response.content else None


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def chat(self, messages, *, tools, response_format=None):
        return dict(self.replies.pop(0))


def call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def final(output):
    return {"role": "assistant", "content": json.dumps(output)}


DIAG = {"diagnosis": "Deploy 2 lowered DB_TIMEOUT_MS from 3000 to 100; bestsellers_30d (~800 ms) now times out.",
        "evidence_refs": ["git:diff:deploy 2", "logs:orders-api"], "confidence": 0.95, "unresolved_questions": []}
PATCH = {"patch_ref": "fix/INC-0001", "diff": REVERT, "evidence_refs": ["sandbox:SBX-0001:tests"], "reproduced": True,
         "passed": True, "test_commands": ["python3 -m pytest services/orders-api/tests -q"],
         "before": {"error_rate_pct": 24.8}, "after": {"error_rate_pct": 0.0}}
REVIEW = {"risk_score": 8, "policy_concerns": [], "decision": "APPROVE",
          "justification": "One-line config revert; reproduced and fixed in sandbox; tests pass.", "evidence_refs": ["sandbox:SBX-0001:diff"]}


async def test_agents_drive_ticket_to_awaiting_approval_then_human_resolves(app, client, auth):
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket_id = (await app.state.db.list_tickets())[0].id

    def tools(role: Role) -> AsgiClient:
        return AsgiClient(role.value, auth(role), client)

    orchestrator = Orchestrator(AsgiClient("orchestrator", auth(Role.orchestrator), client), max_attempts=2)
    orchestrator.agents = {
        "investigator": Investigator(
            llm=ScriptedModel(call("get_git", operation="history"), call("get_git", operation="diff", ref="2"),
                              call("get_logs", service="orders-api", lines=50), final(DIAG)),
            api_client=tools(Role.investigator), on_observation=orchestrator.observer),
        "engineer": Engineer(
            llm=ScriptedModel(call("run_loadtest", ticket_id=ticket_id, requests=200),
                              call("get_code", path="services/orders-api/config/app.env"),
                              call("apply_patch", ticket_id=ticket_id, diff=REVERT),
                              call("run_tests", ticket_id=ticket_id), final(PATCH)),
            api_client=tools(Role.engineer), on_observation=orchestrator.observer),
        "guardian": Guardian(
            llm=ScriptedModel(call("get_diff", ticket_id=ticket_id), call("get_sandbox_results", ticket_id=ticket_id), final(REVIEW)),
            api_client=tools(Role.guardian), on_observation=orchestrator.observer),
    }

    status = await orchestrator.run_ticket(ticket_id)
    assert status == "awaiting_approval"

    view = (await client.get(f"/api/tickets/{ticket_id}", headers=auth(Role.admin))).json()
    assert view["status"] == "awaiting_approval"
    types = [e["type"] for e in view["events"]]
    for expected in ("detected", "finding", "diagnosis", "sandbox_result", "patch", "review", "action_request", "gate_decision"):
        assert expected in types, expected
    findings = [e for e in view["events"] if e["type"] == "finding"]
    assert {f["actor"] for f in findings} >= {"investigator", "engineer", "guardian"}
    assert any(f["payload"].get("tool") == "get_git" and f["payload"]["evidence_ref"].startswith("git:") for f in findings)
    diagnosis = next(e for e in view["events"] if e["type"] == "diagnosis")
    assert diagnosis["actor"] == "investigator" and diagnosis["payload"]["diagnosis"]["confidence"] == 0.95
    gate = next(e for e in view["events"] if e["type"] == "gate_decision")
    assert gate["payload"]["action"] == "set_config" and all(c["ok"] for c in gate["payload"]["checks"])
    statuses = [e["payload"]["to"] for e in view["events"] if e["type"] == "status_change"]
    assert statuses == ["investigating", "fix_proposed", "sandbox_validated", "reviewed", "gate", "awaiting_approval"]

    approved = (await client.post(f"/api/tickets/{ticket_id}/approve", headers=auth(Role.admin))).json()
    await app.state.executor.wait(approved["deployment_id"])
    assert (await client.get(f"/api/tickets/{ticket_id}", headers=auth(Role.admin))).json()["status"] == "resolved"
    assert (await app.state.runner.status()).healthy


async def test_manual_run_endpoint_requires_admin(client, auth):
    created = await client.post("/api/tickets", json={"source": "manual", "service": "orders-api", "title": "x"}, headers=auth(Role.admin))
    ticket_id = created.json()["id"]
    assert (await client.post(f"/api/tickets/{ticket_id}/run", headers=auth(Role.orchestrator))).status_code == 403
    assert (await client.post("/api/tickets/INC-9999/run", headers=auth(Role.admin))).status_code == 404
