import pytest

from oca.fakeco.fake_runner import FakeRunner
from oca.fakeco.runner import RunnerError

pytestmark = pytest.mark.anyio

REVERT = """--- a/services/orders-api/config/app.env
+++ b/services/orders-api/config/app.env
@@ -3,3 +3,3 @@
-DB_TIMEOUT_MS=100
+DB_TIMEOUT_MS=3000
 DB_POOL_MIN=2
"""
INDEX_FIX = """--- /dev/null
+++ b/services/orders-api/migrations/002_orders_customer_email_idx.sql
@@ -0,0 +1 @@
+CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_customer_email ON orders (customer_email);
"""


async def test_timeout_incident_round_trip():
    r = FakeRunner()
    assert (await r.status()).healthy
    r.inject("timeout")
    snap = await r.status()
    assert not snap.healthy and snap.error_rate_pct > 20 and "timeout:bestsellers_30d" in snap.db_errors
    logs = await r.read_logs("orders-api", 100)
    assert any(l.get("msg") == "database request timeout" and l.get("db_timeout_ms") == 100 for l in logs)
    deploys = await r.deployments()
    assert deploys[0].deploy_id == 2 and "fail-fast" in deploys[0].subject
    assert "+DB_TIMEOUT_MS=100" in await r.git_diff(deploys[0].prev_sha, deploys[0].git_sha)
    assert "DB_TIMEOUT_MS=100" in await r.read_file("services/orders-api/config/app.env")

    with pytest.raises(RunnerError):
        await r.sandbox_loadtest(100)
    await r.sandbox_up()
    before = await r.sandbox_loadtest(1000)
    assert before.error_rate_pct > 20 and not (await r.sandbox_tests()).passed

    bad = await r.apply_patch_in_sandbox("fix/INC-0001", "not a diff", "x")
    assert not bad.applied and "git apply" in bad.error
    outside = await r.apply_patch_in_sandbox("fix/INC-0001", REVERT.replace("services/orders-api/config/app.env", "ops/deploy.sh"), "x")
    assert not outside.applied

    good = await r.apply_patch_in_sandbox("fix/INC-0001", REVERT, "Restore DB_TIMEOUT_MS")
    assert good.applied and good.files_changed == ["services/orders-api/config/app.env"]
    after = await r.sandbox_loadtest(1000)
    assert after.error_rate_pct == 0 and (await r.sandbox_tests()).passed
    assert not (await r.status()).healthy  # production untouched

    ref = await r.backup("backup/DEP-0001")
    await r.deploy_branch("fix/INC-0001", "INC-0001")
    assert (await r.status()).healthy
    await r.restore(ref.tag, "restore")
    assert not (await r.status()).healthy


async def test_loyalty_needs_a_migration_and_worse_deploys():
    r = FakeRunner()
    r.inject("loyalty")
    assert "db_pool_utilization_pct" in (await r.status()).breaches
    assert not (await r.health("orders-api"))[0]
    await r.sandbox_up()
    revert = await r.apply_patch_in_sandbox("fix/INC-0002", REVERT, "wrong fix")
    assert revert.applied and (await r.sandbox_loadtest(100)).error_rate_pct > 40
    fix = await r.apply_patch_in_sandbox("fix/INC-0002", INDEX_FIX, "add index")
    assert fix.applied and (await r.sandbox_loadtest(100)).error_rate_pct == 0
    r.failure_modes.add("deploy_makes_worse")
    await r.deploy_branch("fix/INC-0002", "INC-0002")
    assert (await r.status()).error_rate_pct > 60


async def test_read_file_is_jailed():
    r = FakeRunner()
    with pytest.raises(PermissionError):
        await r.read_file("../../etc/passwd")
    with pytest.raises(PermissionError):
        await r.read_file("sandbox.env/../.git/config")
    with pytest.raises(FileNotFoundError):
        await r.read_file("services/orders-api/app/nope.py")
