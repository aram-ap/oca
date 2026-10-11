import pytest

from oca.schemas import Role

pytestmark = pytest.mark.anyio

MATRIX = [
    ("GET", "/api/tools/logs", {"service": "orders-api", "lines": 20}, {Role.investigator}),
    ("GET", "/api/tools/containers", {}, {Role.investigator}),
    ("GET", "/api/tools/metrics", {"service": "orders-api"}, {Role.investigator}),
    ("GET", "/api/tools/code", {"path": "services/orders-api/config/app.env"}, {Role.investigator, Role.engineer}),
    ("GET", "/api/tools/git", {"operation": "history"}, {Role.investigator, Role.engineer}),
]
AGENTS = {Role.investigator, Role.engineer, Role.guardian, Role.orchestrator, Role.health}


async def test_role_matrix(client, auth):
    for method, path, params, allowed in MATRIX:
        for role in AGENTS:
            response = await client.request(method, path, params=params, headers=auth(role))
            expected = 200 if role in allowed else 403
            assert response.status_code == expected, (path, role, response.status_code, response.text)
        assert (await client.request(method, path, params=params)).status_code == 401
        assert (await client.request(method, path, params=params, headers=auth(Role.admin))).status_code == 200


async def test_tool_shapes(app, client, auth):
    app.state.runner.inject("timeout")
    inv = auth(Role.investigator)
    logs = (await client.get("/api/tools/logs", params={"service": "orders-api", "lines": 50}, headers=inv)).json()
    assert logs["lines"] == 50 and logs["error_lines"] > 0 and logs["evidence_ref"].startswith("logs:orders-api")
    assert logs["summary"]["top_errors"][0]["count"] > 0 and "database request timeout" in logs["summary"]["top_errors"][0]["message"]
    assert logs["entries"][0]["status"] == 500 and len(logs["entries"][0]["request_id"]) == 8  # errors first, compacted
    big = (await client.get("/api/tools/logs", params={"service": "orders-api", "lines": 200}, headers=inv)).json()
    assert len(str(big)) < 20_000  # always inside the agents' observation budget
    assert (await client.get("/api/tools/logs", params={"service": "orders-api", "lines": 500}, headers=inv)).status_code == 422
    assert (await client.get("/api/tools/logs", params={"service": "mysql"}, headers=inv)).status_code == 422

    metrics = (await client.get("/api/tools/metrics", params={"service": "orders-api"}, headers=inv)).json()
    assert metrics["series"]["orders_api:error_rate:1m"] > 0.2 and metrics["status"]["healthy"] is False

    code = (await client.get("/api/tools/code", params={"path": "services/orders-api/config/app.env"}, headers=inv)).json()
    assert "DB_TIMEOUT_MS=100" in code["content"]
    assert (await client.get("/api/tools/code", params={"path": "../.git/config"}, headers=inv)).status_code == 403
    assert (await client.get("/api/tools/code", params={"path": "services/x.py"}, headers=inv)).status_code == 404

    history = (await client.get("/api/tools/git", params={"operation": "history"}, headers=inv)).json()
    assert history["deployments"][0]["deploy_id"] == 2 and history["commits"][0]["subject"].startswith("Merge branch")
    diff = (await client.get("/api/tools/git", params={"operation": "diff", "ref": "2"}, headers=inv)).json()
    assert "+DB_TIMEOUT_MS=100" in diff["diff"] and "services/orders-api/config/app.env" in diff["files_changed"]
    latest = (await client.get("/api/tools/git", params={"operation": "diff"}, headers=inv)).json()
    assert latest["ref"] == diff["ref"]
    assert (await client.get("/api/tools/git", params={"operation": "diff", "ref": "zzz"}, headers=inv)).status_code == 404

    containers = (await client.get("/api/tools/containers", headers=inv)).json()
    assert len(containers["containers"]) == 6


async def test_dev_inject_only_in_dry_mode(client, auth):
    assert (await client.post("/api/dev/inject", json={"incident": "timeout"}, headers=auth(Role.orchestrator))).status_code == 403
    assert (await client.post("/api/dev/inject", json={"incident": "timeout"}, headers=auth(Role.admin))).json()["injected"] == "timeout"
    assert (await client.get("/api/dev/state", headers=auth(Role.admin))).json()["effective_incident"] == "timeout"
    assert (await client.post("/api/dev/reset", headers=auth(Role.admin))).json()["reset"] is True
