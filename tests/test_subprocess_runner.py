"""SubprocessRunner argv construction and sha resolution with a stubbed process runner (no docker/git needed)."""

import json

import pytest

from oca.fakeco.runner import DeployRecord, StatusSnapshot
from oca.fakeco.subprocess_runner import SubprocessRunner

pytestmark = pytest.mark.anyio


@pytest.fixture
def runner(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "deploy.sh").write_text("#!/bin/sh\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "deployments.jsonl").write_text(json.dumps(
        {"deploy_id": 13, "ts": "t", "git_sha": "e9ab0544d9", "prev_sha": "f7e46f6a1b", "version": "1.3.0",
         "author": "ops", "subject": "reset to baseline", "files_changed": []}) + "\n")
    r = SubprocessRunner(tmp_path, "http://prom:9090", "http://localhost:19090", "http://localhost:18000", {},
                         data_dir=tmp_path.parent / f"{tmp_path.name}-oca")
    r.commands = []

    async def fake_run(*argv, **kwargs):
        r.commands.append(list(argv))
        if argv[:2] == ("python3", "ops/status.py"):
            return 0, json.dumps({"healthy": False, "git_sha": "f7e46f6", "version": "1.4.0", "error_rate_pct": 12.0,
                                  "p95_latency_ms": 2800, "breaches": ["error_rate_pct"]}), ""
        if argv[:3] == ("git", "rev-parse", "--verify"):
            return (0, "f7e46f6a1b2c\n", "") if "f7e46f6" in argv[-1] else (1, "", "fatal")
        return 0, "", ""
    r._run = fake_run
    return r


async def test_prod_sha_prefers_the_running_service_over_the_deploy_log(runner):
    assert await runner.prod_sha() == "f7e46f6"


async def test_prod_sha_falls_back_to_deploy_log_when_status_is_unusable(runner):
    async def no_status(*argv, **kw):
        runner.commands.append(list(argv))
        if argv[:2] == ("python3", "ops/status.py"):
            return 0, json.dumps({"healthy": True, "git_sha": "unknown"}), ""
        return 0, "", ""
    runner._run = no_status
    assert await runner.prod_sha() == "e9ab0544d9"


async def test_refs_are_validated_before_reaching_git(runner):
    from oca.fakeco.runner import RunnerError
    with pytest.raises(RunnerError):
        await runner.git_diff("--output=/tmp/x", "HEAD")
    with pytest.raises(RunnerError):
        await runner.apply_patch_in_sandbox("main", "diff", "msg")


@pytest.fixture
def git_repo(tmp_path):
    """A tiny company repo. OCA creates its own worktree outside this checkout."""
    import subprocess

    root = tmp_path / "driftwood"
    (root / "services" / "orders-api" / "config").mkdir(parents=True)
    (root / "ops").mkdir()
    (root / "ops" / "deploy.sh").write_text("#!/bin/sh\n")
    (root / "services" / "orders-api" / "config" / "app.env").write_text(
        "# orders-api runtime config. Deployed with the service: change it with a commit,\n# then run ops/deploy.sh.\n"
        "DB_TIMEOUT_MS=100\nDB_POOL_MIN=2\nDB_POOL_MAX=10\nDB_POOL_ACQUIRE_TIMEOUT_MS=2000\nPRODUCTS_CACHE_TTL_S=60\nBESTSELLERS_CACHE_TTL_S=30\n")

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    return root


WRONG_COUNTS = """--- a/services/orders-api/config/app.env
+++ b/services/orders-api/config/app.env
@@ -1,9 +1,9 @@
 # orders-api runtime config. Deployed with the service: change it with a commit,
 # then run ops/deploy.sh.
-DB_TIMEOUT_MS=100
+DB_TIMEOUT_MS=3000
 DB_POOL_MIN=2
"""
FENCED_NO_PREFIX = """```diff
--- services/orders-api/config/app.env
+++ services/orders-api/config/app.env
@@ -3 +3 @@
-DB_TIMEOUT_MS=3000
+DB_TIMEOUT_MS=2500
```"""


async def test_real_git_apply_tolerates_model_diffs(git_repo):
    r = SubprocessRunner(git_repo, "http://prom:9090", "http://localhost:19090", "http://localhost:18000", {},
                         data_dir=git_repo.parent / "oca-data")
    r.worktree.parent.mkdir(parents=True)
    await r._run("git", "worktree", "add", "-q", "--detach", str(r.worktree), "HEAD")
    result = await r.apply_patch_in_sandbox("fix/INC-0001", WRONG_COUNTS, "revert timeout")
    assert result.applied, result.error
    assert result.files_changed == ["services/orders-api/config/app.env"] and "--recount" in result.stat
    assert "DB_TIMEOUT_MS=3000" in (r.worktree / "services/orders-api/config/app.env").read_text()
    assert "DB_TIMEOUT_MS=100" in (git_repo / "services/orders-api/config/app.env").read_text()  # production untouched
    assert "+DB_TIMEOUT_MS=3000" in await r.sandbox_diff("fix/INC-0001")

    # Second patch on the same branch: fenced, no a/ b/ prefixes, single-line hunk.
    again = await r.apply_patch_in_sandbox("fix/INC-0001", FENCED_NO_PREFIX, "tune")
    assert again.applied, again.error
    assert "DB_TIMEOUT_MS=2500" in (r.worktree / "services/orders-api/config/app.env").read_text()

    garbage = await r.apply_patch_in_sandbox("fix/INC-0001", "--- a/services/orders-api/config/app.env\n+++ b/services/orders-api/config/app.env\n@@ -1 +1 @@\n-NOT IN FILE\n+X\n", "bad")
    assert not garbage.applied and "does not apply" in garbage.error


def test_prometheus_nan_is_no_data():
    from oca.fakeco.prometheus import finite
    assert finite("NaN") is None and finite("+Inf") is None and finite("0.25") == 0.25 and finite(None) is None
