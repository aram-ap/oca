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
    r = SubprocessRunner(tmp_path, "http://prom:9090", "http://prom:19090", "http://api:18000", {})
    r.commands = []

    async def fake_run(*argv, cwd=None, timeout=120, env=None, stdin=None, check=True):
        r.commands.append(list(argv))
        if argv[:2] == ("python3", "ops/status.py"):
            return 0, json.dumps({"healthy": False, "git_sha": "f7e46f6", "version": "1.4.0", "error_rate_pct": 12.0,
                                  "p95_latency_ms": 2800, "breaches": ["error_rate_pct"]}), ""
        if argv[:3] == ("git", "rev-parse", "--verify"):
            return (0, "f7e46f6a1b2c\n", "") if "f7e46f6" in argv[-1] else (1, "", "fatal")
        if argv[:2] == ("ops/sandbox.sh", "up"):
            return 0, 'progress...\n{"sandbox": "up", "git_sha": "f7e46f6a1b", "version": "1.4.0"}\n', ""
        return 0, "", ""
    r._run = fake_run
    return r


async def test_prod_sha_prefers_the_running_service_over_the_deploy_log(runner):
    assert await runner.prod_sha() == "f7e46f6"
    info = await runner.sandbox_up("f7e46f6")
    assert info.git_sha == "f7e46f6a1b" and ["ops/sandbox.sh", "up", "f7e46f6"] in runner.commands


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
