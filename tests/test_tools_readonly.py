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
    prefix = (await client.get("/api/tools/git", params={"operation": "diff", "ref": "f6e5d4c"}, headers=inv)).json()
    assert "+DB_TIMEOUT_MS=100" in prefix["diff"]  # a short sha, as models like to quote them

    containers = (await client.get("/api/tools/containers", headers=inv)).json()
    assert len(containers["containers"]) == 6


async def test_dev_inject_only_in_dry_mode(client, auth):
    assert (await client.post("/api/dev/inject", json={"incident": "timeout"}, headers=auth(Role.orchestrator))).status_code == 403
    assert (await client.post("/api/dev/inject", json={"incident": "timeout"}, headers=auth(Role.admin))).json()["injected"] == "timeout"
    assert (await client.get("/api/dev/state", headers=auth(Role.admin))).json()["effective_incident"] == "timeout"
    assert (await client.post("/api/dev/reset", headers=auth(Role.admin))).json()["reset"] is True
    calls = (await client.get("/api/dev/runner-calls", headers=auth(Role.admin))).json()
    assert calls["mode"] == "dry" and isinstance(calls["calls"], list)


async def test_git_refs_accept_evidence_labels_and_flag_maintenance(app, client, auth):
    runner, eng = app.state.runner, auth(Role.engineer)
    runner.inject("timeout")
    history = (await client.get("/api/tools/git", params={"operation": "history"}, headers=eng)).json()
    latest = history["latest_deployment"]
    assert latest["git_sha"] == history["deployments"][0]["git_sha"] and latest["maintenance"] is False
    diff = (await client.get("/api/tools/git", params={"operation": "diff", "ref": latest["git_sha"]}, headers=eng)).json()
    label = diff["evidence_ref"]  # git:diff:deploy N (prev..sha)
    assert label.startswith("git:diff:deploy ")
    for copied in (label, label.removeprefix("git:diff:"), f"deploy {latest['deploy_id']}", label.split("(")[1].rstrip(")")):
        again = await client.get("/api/tools/git", params={"operation": "diff", "ref": copied}, headers=eng)
        assert again.status_code == 200 and again.json()["diff"] == diff["diff"], copied
    bad = await client.get("/api/tools/git", params={"operation": "diff", "ref": "deploy (what is this)"}, headers=eng)
    assert bad.status_code == 404 and "Unusable ref" in bad.json()["detail"]
    assert (await client.get("/api/tools/git", params={"operation": "diff", "ref": "--output=/tmp/x"}, headers=eng)).status_code == 404
    from oca.api.tools import _is_maintenance, _normalise_ref
    from oca.fakeco.runner import DeployRecord
    reset = DeployRecord(9, "t", "aaaaaaaaaa", "bbbbbbbbbb", "1.3.0", "ops", "Baseline", [], "reset to baseline")
    assert _is_maintenance(reset) and not _is_maintenance(history["deployments"][0] and reset.__class__(
        *[*reset.__dict__.values()][:-1], None))
    assert _normalise_ref("git:diff:deploy 26 (59d2e30336..9114e82260)") == "26"
    assert _normalise_ref("(59d2e30336..9114e82260)") == "59d2e30336..9114e82260" and _normalise_ref(" 1e30750a67 ") == "1e30750a67"
