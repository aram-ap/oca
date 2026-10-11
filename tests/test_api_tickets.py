import json

import pytest

from oca.schemas import Role

pytestmark = pytest.mark.anyio


async def test_health_is_public(client):
    response = await client.get("/api/health")
    assert response.status_code == 200 and response.json()["runner"] == "dry"


async def test_ticket_crud_events_and_stream(client, auth):
    body = {"source": "manual", "service": "orders-api", "title": "Checkout failing", "description": "customers report"}
    assert (await client.post("/api/tickets", json=body)).status_code == 401
    created = await client.post("/api/tickets", json=body, headers=auth(Role.admin))
    assert created.status_code == 201
    ticket_id = created.json()["id"]
    assert ticket_id == "INC-0001"

    listed = await client.get("/api/tickets", headers=auth(Role.guardian))
    assert [t["id"] for t in listed.json()] == [ticket_id]

    # Agents append findings; the actor is forced to the caller's role.
    forged = await client.post(f"/api/tickets/{ticket_id}/events",
                               json={"type": "finding", "payload": {"actor": "gate", "note": "hi"}},
                               headers=auth(Role.investigator))
    assert forged.status_code == 201 and forged.json()["actor"] == "investigator"
    platform_only = await client.post(f"/api/tickets/{ticket_id}/events", json={"type": "gate_decision"},
                                      headers=auth(Role.orchestrator))
    assert platform_only.status_code == 403

    # Only the orchestrator moves pipeline states, and only pipeline states.
    assert (await client.post(f"/api/tickets/{ticket_id}/status", json={"status": "investigating"},
                              headers=auth(Role.engineer))).status_code == 403
    assert (await client.post(f"/api/tickets/{ticket_id}/status", json={"status": "approved"},
                              headers=auth(Role.orchestrator))).status_code == 403
    moved = await client.post(f"/api/tickets/{ticket_id}/status", json={"status": "investigating", "attempt": 1},
                              headers=auth(Role.orchestrator))
    assert moved.status_code == 200 and moved.json()["status"] == "investigating" and moved.json()["attempt"] == 1

    full = await client.get(f"/api/tickets/{ticket_id}", headers=auth(Role.orchestrator))
    types = [e["type"] for e in full.json()["events"]]
    assert types == ["reported", "finding", "status_change"]

    # SSE replay: three events already stored, ask for the first two after id 0.
    stream = await client.get(f"/api/tickets/{ticket_id}/stream", params={"limit": 2, "timeout_s": 1},
                              headers=auth(Role.admin))
    assert stream.headers["content-type"].startswith("text/event-stream")
    chunks = [c for c in stream.text.split("\n\n") if c.startswith("event:")]
    assert len(chunks) == 2
    first = json.loads(chunks[0].split("data: ", 1)[1])
    assert first["type"] == "reported" and "id: 1" in chunks[0]

    # Last-Event-ID resumes after the given id.
    resumed = await client.get(f"/api/tickets/{ticket_id}/stream", params={"limit": 1, "timeout_s": 1},
                               headers={**auth(Role.admin), "Last-Event-ID": "2"})
    assert "id: 3" in resumed.text

    assert (await client.get("/api/tickets/INC-9999", headers=auth(Role.admin))).status_code == 404


async def test_settings_roundtrip_and_validation(client, auth):
    view = await client.get("/api/settings", headers=auth(Role.investigator))
    assert view.json()["require_human_approval"] is True and view.json()["risk_threshold"] == 30
    assert (await client.put("/api/settings", json={"risk_threshold": 50}, headers=auth(Role.orchestrator))).status_code == 403
    assert (await client.put("/api/settings", json={"risk_threshold": 150}, headers=auth(Role.admin))).status_code == 422
    assert (await client.put("/api/settings", json={"runner": "real"}, headers=auth(Role.admin))).status_code == 422
    updated = await client.put("/api/settings", json={"risk_threshold": 50, "require_human_approval": False},
                               headers=auth(Role.admin))
    assert updated.json()["risk_threshold"] == 50 and updated.json()["require_human_approval"] is False


async def test_local_ai_stats(client, auth):
    stats = await client.get("/api/stats/local-ai", headers=auth(Role.admin))
    data = stats.json()
    assert data["cloud_requests"] == 0 and data["bytes_uploaded"] == 0 and data["inference"] == "100% LOCAL"
    assert data["ticketing_mode"].startswith("not connected")
