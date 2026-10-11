import pytest

from oca.fakeco import diffcheck
from tests.test_fake_runner import INDEX_FIX, REVERT


def test_parse_and_flags():
    info = diffcheck.validate(REVERT)
    assert info.files == ["services/orders-api/config/app.env"] and info.config_changes == {"DB_TIMEOUT_MS": "3000"}
    flags = diffcheck.risk_flags(REVERT)
    assert flags.config_only and not flags.db_migration and flags.files_changed == 1
    mig = diffcheck.validate(INDEX_FIX)
    assert mig.new_files == ["services/orders-api/migrations/002_orders_customer_email_idx.sql"]
    assert diffcheck.risk_flags(INDEX_FIX).db_migration


@pytest.mark.parametrize("bad", [
    "hello", REVERT.replace("services/orders-api/", "ops/"), REVERT.replace("app.env", "../../x.env"),
    REVERT.replace("config/app.env", "requirements.txt"),
])
def test_rejects(bad):
    with pytest.raises(ValueError):
        diffcheck.validate(bad)
