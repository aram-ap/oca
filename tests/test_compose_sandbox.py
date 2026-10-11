"""Real git worktrees and recorded Docker calls; no running Docker daemon needed."""

import json
import subprocess

import pytest

from oca.config import Settings
from oca.environments.compose import ComposeSandbox
from oca.environments.profile import load_profile
from oca.fakeco.runner import RunnerError
from oca.fakeco.subprocess_runner import SubprocessRunner

pytestmark = pytest.mark.anyio


def company_model(checkout):
    """Canonical shape returned by Compose config for Driftwood's six services."""
    return {
        "name": "driftwood",
        "volumes": {"pgdata": {"name": "driftwood_pgdata"}},
        "networks": {"default": {"name": "driftwood_default"}},
        "services": {
            "postgres": {"image": "postgres:16-alpine", "environment": {
                "POSTGRES_PASSWORD": "production-secret", "POSTGRES_USER": "driftwood",
                "POSTGRES_DB": "driftwood", "SEED_ORDERS": "2000000"},
                "volumes": [{"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"},
                            {"type": "bind", "source": str(checkout / "db/init"), "target": "/docker-entrypoint-initdb.d"}],
                "ports": [{"target": 5432, "published": "5432"}]},
            "redis": {"image": "redis:7-alpine", "ports": [{"target": 6379, "published": "6379"}]},
            "orders-api": {"build": {"context": str(checkout / "services/orders-api"), "dockerfile": "Dockerfile"},
                "environment": {"DATABASE_URL": "postgresql://production-secret@prod/db", "DB_TIMEOUT_MS": "100", "VENDOR_TOKEN": "secret"},
                "volumes": [{"type": "bind", "source": "/production/logs", "target": "/var/log/driftwood"}],
                "ports": [{"target": 8000, "published": "8000"}]},
            "frontend": {"image": "nginx:1.27-alpine", "labels": {"traefik.enable": "true"},
                "volumes": [{"type": "bind", "source": str(checkout / "services/frontend/html"), "target": "/usr/share/nginx/html"}],
                "ports": [{"target": 80, "published": "8080"}]},
            "prometheus": {"image": "prom/prometheus:v3.1.0", "ports": [{"target": 9090, "published": "9090"}]},
            "loadgen": {"build": {"context": str(checkout / "services/loadgen")}, "deploy": {"replicas": 1}},
        },
    }


@pytest.fixture
def sandbox_runner(tmp_path):
    root = tmp_path / "company"
    for path in ("ops", "db/init", "services/orders-api/tests", "services/frontend/html", "services/loadgen"):
        (root / path).mkdir(parents=True)
    (root / "ops/deploy.sh").write_text("#!/bin/sh\n")
    (root / "services/orders-api/VERSION").write_text("1.3.1\n")
    (root / "services/orders-api/tests/test_smoke.py").write_text("def test_original(): pass\n")
    (root / "docker-compose.yml").write_text("services:\n  orders-api:\n    image: example\n")
    (root / ".env").write_text("POSTGRES_PASSWORD=production-secret\n")
    for args in (("init", "-q", "-b", "main"), ("add", "."), ("commit", "-q", "-m", "baseline")):
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@local", *args], cwd=root,
                       check=True, capture_output=True)
    runner = SubprocessRunner(root, "http://localhost:9090", "http://localhost:19090",
                              "http://localhost:18000", {}, data_dir=tmp_path / "oca-data")
    original_run = runner._run
    runner.commands = []
    runner.fail_up = False
    runner.fail_down = False
    runner.config_transform = lambda value: value

    async def recorded_run(*argv, **kwargs):
        runner.commands.append((argv, kwargs))
        if argv[0] != "docker":
            return await original_run(*argv, **kwargs)
        if "config" in argv:
            return 0, json.dumps(runner.config_transform(company_model(runner.worktree))), ""
        if "up" in argv and runner.fail_up:
            raise RunnerError("startup failed")
        if "down" in argv and runner.fail_down:
            raise RunnerError("cleanup failed")
        if "--requests=100" in argv:
            return 0, json.dumps({"requests": 100, "failures": 25, "error_rate_pct": 25, "p95_ms": 10000}), ""
        if "pytest" in argv:
            return 0, "2 passed in 0.5s", ""
        return 0, "", ""

    runner._run = recorded_run
    return runner


async def test_lifecycle_owns_resources_without_company_sandbox_scripts(sandbox_runner, monkeypatch):
    r = sandbox_runner
    monkeypatch.setenv("POSTGRES_PASSWORD", "host-secret")
    monkeypatch.setenv("OCA_ENGINEER_TOKEN", "agent-token")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")
    original_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=r.root).decode().strip()
    info = await r.sandbox_up("HEAD")
    assert info.git_sha == original_sha and info.version == "1.3.1"
    assert r.worktree.is_relative_to(r.sandbox.runtime) and not r.worktree.is_relative_to(r.root)
    model = json.loads(r.sandbox.model_path.read_text())
    services = model["services"]
    assert model["name"].startswith("oca-sandbox-")
    assert model["volumes"]["pgdata"]["name"] != "driftwood_pgdata"
    assert model["networks"]["default"]["internal"] is True
    # Internal networks never publish host ports, so published services also join a non-internal bridge.
    assert model["networks"]["oca-published"] == {"name": f"{r.sandbox.project}_oca-published", "driver": "bridge"}
    for name in ("orders-api", "prometheus", "frontend"):
        assert set(services[name]["networks"]) == {"default", "oca-published"}
    for name in ("postgres", "redis", "loadgen"):
        assert services[name]["networks"] == {"default": {}}
    assert services["postgres"]["ports"] == services["redis"]["ports"] == []
    assert services["orders-api"]["ports"][0]["published"] == "18000"
    assert services["orders-api"]["ports"][0]["host_ip"] == "127.0.0.1"
    password = services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert password != "production-secret" and password in services["orders-api"]["environment"]["DATABASE_URL"]
    assert services["orders-api"]["environment"]["DB_TIMEOUT_MS"] == "100"
    assert "VENDOR_TOKEN" not in services["orders-api"]["environment"]
    assert "labels" not in services["frontend"]
    assert services["postgres"]["volumes"][1]["read_only"]
    assert services["orders-api"]["volumes"][0]["source"] == str(r.sandbox.logs)
    assert services["loadgen"]["deploy"]["replicas"] == 0
    assert services["loadgen"]["profiles"] == ["oca-loadtesting"]

    # Rebuilding retains the sandbox DB credential and checkout; the production checkout is unchanged.
    first_checkout = r.worktree
    await r.sandbox_up("HEAD")
    assert r.worktree == first_checkout
    assert json.loads(r.sandbox.model_path.read_text())["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] == password
    assert (await r.sandbox_loadtest(100)).error_rate_pct == 25
    assert (await r.sandbox_tests()).total == 2
    for argv, kwargs in r.commands:
        assert "ops/sandbox.sh" not in argv and "ops/reset_db.sh" not in argv
        if argv[0] == "docker":
            assert kwargs["cwd"] == r.worktree and kwargs["inherit_env"] is False
            assert not {"POSTGRES_PASSWORD", "OCA_ENGINEER_TOKEN", "HTTP_PROXY"} & kwargs["env"].keys()
            assert "--env-file" in argv and str(r.root / ".env") not in argv
            assert argv[argv.index("-p") + 1] == r.sandbox.project
    await r.sandbox_reset_db()
    assert r.worktree == first_checkout
    await r.sandbox_down()
    assert_torn_down(r)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=r.root).decode().strip() == original_sha
    assert "production-secret" in (r.root / ".env").read_text()
    assert (r.root / "services/orders-api/tests/test_smoke.py").read_text() == "def test_original(): pass\n"
    assert not (r.root / ".sandbox").exists()
    await r.sandbox_down()  # idempotent

    # Docker Desktop refuses a bind path that was deleted and recreated, so the next sandbox gets new
    # checkout/log directories under the persistent runtime directory, and a new credential.
    await r.sandbox_up("HEAD")
    assert r.worktree != first_checkout and r.worktree.is_dir() and r.worktree.parent == r.sandbox.runtime
    model = json.loads(r.sandbox.model_path.read_text())
    assert model["services"]["orders-api"]["volumes"][0]["source"] == str(r.sandbox.logs) != str(first_checkout.parent / "logs")
    assert model["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] != password
    assert model["services"]["oca-tests"]["volumes"][0]["source"] == str(r.worktree)
    await r.sandbox_down()
    assert_torn_down(r)


def assert_torn_down(r):
    """Containers' bind sources and generated files are gone; the runtime directory and owner record stay."""
    assert not r.worktree.exists() and not r.sandbox.logs.exists()
    assert not r.sandbox.model_path.exists() and not r.sandbox.env_file.exists()
    assert r.sandbox.runtime.is_dir() and r.sandbox.marker.exists()
    assert not any(line.strip().endswith(r.sandbox.runtime.name) for line in
                   subprocess.check_output(["git", "worktree", "list"], cwd=r.root).decode().splitlines()[1:])


async def test_tests_read_the_patch_checkout_in_an_isolated_container(sandbox_runner):
    r = sandbox_runner
    await r.sandbox_up("HEAD")
    (r.worktree / "services/orders-api/tests/test_smoke.py").write_text("def test_patched(): pass\n")
    result = await r.sandbox_tests()
    assert result.passed
    model = json.loads(r.sandbox.model_path.read_text())
    service = model["services"]["oca-tests"]
    assert service["volumes"][0]["source"] == str(r.worktree) and service["volumes"][0]["read_only"]
    assert service["environment"]["API_URL"] == "http://orders-api:8000"
    assert service["read_only"] and service["networks"] == {"default": {}}
    assert "test_original" in (r.root / "services/orders-api/tests/test_smoke.py").read_text()
    await r.sandbox_down()


@pytest.mark.parametrize("unsafe", ["host_network", "socket", "external_volume", "external_network", "volume_driver", "build_escape", "privileged", "secrets"])
async def test_unsafe_compose_models_are_rejected_before_startup(sandbox_runner, unsafe):
    r = sandbox_runner

    def change(model):
        api = model["services"]["orders-api"]
        if unsafe == "host_network":
            api["network_mode"] = "host"
        elif unsafe == "socket":
            api["volumes"].append({"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"})
        elif unsafe == "external_volume":
            model["volumes"]["pgdata"]["external"] = True
        elif unsafe == "external_network":
            model["networks"]["default"]["external"] = True
        elif unsafe == "volume_driver":
            model["volumes"]["pgdata"]["driver_opts"] = {"device": "/production/data"}
        elif unsafe == "build_escape":
            api["build"]["context"] = str(r.root)
        elif unsafe == "privileged":
            api["privileged"] = True
        else:
            model["secrets"] = {"prod": {"file": "/production/secret"}}
        return model

    r.config_transform = change
    with pytest.raises(RunnerError):
        await r.sandbox_up("HEAD")
    assert not any(argv[0] == "docker" and "up" in argv for argv, _ in r.commands)
    assert_torn_down(r)


async def test_partial_startup_is_cleaned_and_cleanup_failure_can_be_retried(sandbox_runner):
    r = sandbox_runner
    r.fail_up = True
    with pytest.raises(RunnerError, match="startup failed"):
        await r.sandbox_up("HEAD")
    assert_torn_down(r)
    assert any("down" in argv for argv, _ in r.commands)
    r.fail_up = False
    await r.sandbox_up("HEAD")
    r.fail_down = True
    with pytest.raises(RunnerError, match="cleanup failed"):
        await r.sandbox_down()
    assert r.sandbox.marker.exists() and r.worktree.exists()
    r.fail_down = False
    await r.sandbox_down()
    assert_torn_down(r)


async def test_cleanup_refuses_an_unowned_directory(sandbox_runner):
    r = sandbox_runner
    r.sandbox.runtime.mkdir(parents=True)
    sentinel = r.sandbox.runtime / "important.txt"
    sentinel.write_text("keep")
    with pytest.raises(RunnerError, match="ownership"):
        await r.sandbox_down()
    assert sentinel.read_text() == "keep" and not r.commands


async def test_cleanup_can_remove_containers_when_checkout_is_already_gone(sandbox_runner):
    r = sandbox_runner
    await r.sandbox_up("HEAD")
    await r._run("git", "worktree", "remove", "--force", str(r.worktree))
    await r.sandbox_down()
    assert_torn_down(r)
    argv, kwargs = next((argv, kwargs) for argv, kwargs in reversed(r.commands) if "down" in argv)
    assert kwargs["cwd"] == r.sandbox.runtime


def test_profile_and_location_validation(tmp_path):
    profile = load_profile()
    with pytest.raises(ValueError):
        type(profile).model_validate({**profile.model_dump(), "test_path": "../production"})
    with pytest.raises(ValueError):
        type(profile).model_validate({**profile.model_dump(), "shell_command": "anything"})
    with pytest.raises(RunnerError, match="outside"):
        ComposeSandbox(tmp_path, tmp_path / "data", profile, "http://localhost:18000", "http://localhost:19090", None)
    with pytest.raises(RunnerError, match="loopback"):
        ComposeSandbox(tmp_path, tmp_path.parent / "data", profile, "http://production:8000", "http://localhost:19090", None)
    settings = Settings.from_env({"OCA_SANDBOX_PROFILE": str(tmp_path / "profile.json")}, dotenv=None)
    assert settings.sandbox_profile == tmp_path / "profile.json"


async def test_failed_measurement_cleans_the_environment(app, monkeypatch):
    from oca.schemas import TicketCreate

    ticket = await app.state.db.create_ticket(TicketCreate(source="manual", service="orders-api", title="test incident"))

    async def no_measurement(*args):
        raise RunnerError("measurement failed")

    monkeypatch.setattr(app.state.runner, "sandbox_loadtest", no_measurement)
    with pytest.raises(RunnerError, match="measurement failed"):
        await app.state.sandbox.prepare(ticket.id)
    assert not app.state.runner.sandbox["up"] and app.state.sandbox.active_ticket is None


async def test_teardown_failure_is_reported_and_remains_retryable(app, client, auth, monkeypatch):
    from oca.schemas import Role, TicketCreate

    ticket = await app.state.db.create_ticket(TicketCreate(source="manual", service="orders-api", title="test incident"))
    app.state.sandbox.active_ticket = ticket.id
    original = app.state.runner.sandbox_down

    async def fail_cleanup():
        raise RunnerError("cleanup failed")

    monkeypatch.setattr(app.state.runner, "sandbox_down", fail_cleanup)
    response = await client.post(f"/api/sandbox/{ticket.id}/teardown", headers=auth(Role.orchestrator))
    assert response.status_code == 502
    assert app.state.sandbox.active_ticket == ticket.id
    events = await app.state.db.list_events(ticket.id)
    assert any(event.payload.get("where") == "sandbox.teardown" for event in events)
    monkeypatch.setattr(app.state.runner, "sandbox_down", original)
    await app.state.sandbox.teardown(ticket.id)
    assert app.state.sandbox.active_ticket is None
