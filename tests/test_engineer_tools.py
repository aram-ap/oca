"""Structured Engineer write tools: the server writes the diff, records it, and lists files."""

from __future__ import annotations

import pytest

from oca.schemas import EventType, Role, TicketStatus

pytestmark = pytest.mark.anyio


async def _prepared_ticket(app, client, auth):
    app.state.runner.inject("timeout")
    await app.state.scheduler.tick(force=True)
    ticket = (await app.state.db.list_tickets())[0]
    prepared = await client.post(f"/api/sandbox/{ticket.id}/prepare", headers=auth(Role.orchestrator))
    assert prepared.status_code == 200 and prepared.json()["reproduced"] is True
    return ticket


async def test_set_config_writes_and_applies_the_diff(app, client, auth):
    ticket = await _prepared_ticket(app, client, auth)
    eng = auth(Role.engineer)
    files = (await client.get("/api/tools/files", params={"path": "services/orders-api"}, headers=eng)).json()
    assert "services/orders-api/config/app.env" in files["files"] and files["count"] >= 4
    assert (await client.get("/api/tools/files", params={"path": "ops"}, headers=eng)).status_code in (403, 404)

    before = (await client.get("/api/tools/code", params={"path": "services/orders-api/config/app.env"}, headers=eng)).json()
    assert "DB_TIMEOUT_MS=100" in before["content"]
    result = (await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "DB_TIMEOUT_MS", "value": 3000},
                                headers=eng)).json()
    assert result["applied"] is True and result["patch_ref"] == f"fix/{ticket.id}" and result["risk_flags"]["config_only"]
    assert "-DB_TIMEOUT_MS=100" in result["diff"] and "+DB_TIMEOUT_MS=3000" in result["diff"]
    assert result["diff"].startswith("--- a/services/orders-api/config/app.env\n+++ b/services/orders-api/config/app.env\n@@ -")

    after = (await client.get("/api/tools/code", params={"path": "services/orders-api/config/app.env"}, headers=eng)).json()
    assert "DB_TIMEOUT_MS=3000" in after["content"]  # the sandbox view reflects the fix branch
    repeat = await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "DB_TIMEOUT_MS", "value": 3000}, headers=eng)
    assert repeat.status_code == 409 and "already" in repeat.json()["detail"]
    bad = await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "db timeout", "value": 1}, headers=eng)
    assert bad.status_code == 409

    load = (await client.post("/api/tools/sandbox/loadtest", json={"ticket_id": ticket.id}, headers=eng)).json()
    assert load["phase"] == "after" and load["metrics"]["error_rate_pct"] == 0.0
    validated = (await client.post(f"/api/sandbox/{ticket.id}/validate", headers=auth(Role.orchestrator))).json()
    assert validated["passed"] is True
    findings = [e for e in await app.state.db.list_events(ticket.id) if e.type is EventType.finding and e.payload.get("tool") == "set_config"]
    assert len(findings) == 1 and findings[0].payload["applied"] and "+DB_TIMEOUT_MS=3000" in findings[0].payload["diff"]
    assert findings[0].payload["diff_truncated"] is False
    assert (await client.post("/api/tools/sandbox/config", json={"ticket_id": ticket.id, "key": "X", "value": 1},
                              headers=auth(Role.investigator))).status_code == 403


async def test_create_migration_numbers_the_file(app, client, auth):
    ticket = await _prepared_ticket(app, client, auth)
    eng = auth(Role.engineer)
    sql = "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_customer_email ON orders (customer_email);"
    result = (await client.post("/api/tools/sandbox/migration", json={"ticket_id": ticket.id, "name": "orders_customer_email_idx", "sql": sql},
                                headers=eng)).json()
    assert result["applied"] is True and result["path"] == "services/orders-api/migrations/002_orders_customer_email_idx.sql"
    assert result["risk_flags"]["db_migration"] is True and result["diff"].startswith("--- /dev/null\n+++ b/services/orders-api/migrations/002_")
    listing = (await client.get("/api/tools/files", params={"path": "services/orders-api/migrations"}, headers=eng)).json()
    assert "services/orders-api/migrations/002_orders_customer_email_idx.sql" in listing["files"]
    duplicate = await client.post("/api/tools/sandbox/migration", json={"ticket_id": ticket.id, "name": "003_orders_customer_email_idx.sql", "sql": sql}, headers=eng)
    assert duplicate.status_code == 409
    empty = await client.post("/api/tools/sandbox/migration", json={"ticket_id": ticket.id, "name": "x", "sql": "   "}, headers=eng)
    assert empty.status_code == 409


async def test_rejected_patch_is_recorded_with_its_diff(app, client, auth):
    ticket = await _prepared_ticket(app, client, auth)
    bad = "--- a/services/orders-api/Dockerfile\n+++ b/services/orders-api/Dockerfile\n@@ -1 +1 @@\n-FROM python:3.12\n+FROM python:3.13\n"
    rejected = (await client.post("/api/tools/sandbox/patch", json={"ticket_id": ticket.id, "diff": bad}, headers=auth(Role.engineer))).json()
    assert rejected["applied"] is False and rejected["hint"]
    finding = [e for e in await app.state.db.list_events(ticket.id) if e.payload.get("tool") == "apply_patch"][-1]
    assert finding.payload["applied"] is False and finding.payload["diff"] == bad
    assert (await app.state.db.get_ticket(ticket.id)).status is TicketStatus.detected
