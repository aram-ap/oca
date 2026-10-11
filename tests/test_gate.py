from datetime import datetime, timedelta, timezone

from oca.config import Settings
from oca.gate import GateContext, evaluate, load_allowlist
from oca.schemas import (
    ActionName, ActionRequest, ActionStatus, Deployment, DeploymentStatus, Evidence, GuardianDecision, Review,
    SandboxMetrics, SandboxRun, TestRunResult, utcnow,
)

ALLOWLIST = load_allowlist(Settings().allowlist_path)
NOW = utcnow()


def run(reproduced=True, passed=True, tests_passed=True):
    return SandboxRun(id="SBX-0001", ticket_id="INC-0001", patch_ref="fix/INC-0001", base_sha="f6e5d4c3b2",
                      before=SandboxMetrics(requests=100, failures=25, error_rate_pct=25.0, p95_ms=10000),
                      after=SandboxMetrics(requests=100, failures=0, error_rate_pct=0.0, p95_ms=16),
                      tests=TestRunResult(passed=tests_passed, total=7, failed=0 if tests_passed else 1),
                      reproduced=reproduced, passed=passed, created_at=NOW, updated_at=NOW)


def review(decision=GuardianDecision.APPROVE, risk=8):
    return Review(risk_score=risk, policy_concerns=[], decision=decision, justification="ok", evidence_refs=["sandbox:SBX-0001"])


def ctx(**kw):
    base = dict(allowlist=ALLOWLIST, sandbox_run=run(), review=review(), require_human_approval=True)
    base.update(kw)
    return GateContext(**base)


DEPLOY = ActionRequest(action=ActionName.deploy_patch, args={"ticket_id": "INC-0001", "branch": "fix/INC-0001"}, ticket_id="INC-0001")


def names(decision, ok):
    return [c.name for c in decision.checks if c.ok is ok]


def test_all_green_waits_for_human_by_default():
    decision = evaluate(DEPLOY, ctx())
    assert decision.status is ActionStatus.awaiting_approval and decision.requires_human
    assert len(decision.checks) == 8 and all(c.ok for c in decision.checks)


def test_autonomous_mode_passes_policy():
    decision = evaluate(DEPLOY, ctx(require_human_approval=False))
    assert decision.status is ActionStatus.policy_pass and not decision.requires_human


def test_unknown_action_and_bad_args():
    bad = ActionRequest(action=ActionName.set_config, args={"service": "orders-api", "key": "DB_TIMEOUT_MS"}, ticket_id="INC-0001")
    decision = evaluate(bad, ctx())
    assert decision.status is ActionStatus.denied and names(decision, False) == ["args_match_schema"]
    assert names(decision, None)[0] == "args_within_values"  # later checks skipped
    out_of_range = ActionRequest(action=ActionName.set_config, args={"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 5}, ticket_id="INC-0001")
    assert names(evaluate(out_of_range, ctx()), False) == ["args_within_values"]
    bad_key = ActionRequest(action=ActionName.set_config, args={"service": "orders-api", "key": "SECRET", "value": 500}, ticket_id="INC-0001")
    assert "SECRET" in evaluate(bad_key, ctx()).reason
    wrong_branch = ActionRequest(action=ActionName.deploy_patch, args={"ticket_id": "INC-0001", "branch": "main"}, ticket_id="INC-0001")
    assert names(evaluate(wrong_branch, ctx()), False) == ["args_within_values"]


def test_evidence_checks_in_order():
    assert names(evaluate(DEPLOY, ctx(sandbox_run=None)), False) == ["sandbox_reproduced_and_fixed"]
    assert names(evaluate(DEPLOY, ctx(sandbox_run=run(reproduced=False))), False) == ["sandbox_reproduced_and_fixed"]
    assert names(evaluate(DEPLOY, ctx(sandbox_run=run(passed=False))), False) == ["sandbox_reproduced_and_fixed"]
    assert names(evaluate(DEPLOY, ctx(sandbox_run=run(tests_passed=False))), False) == ["tests_passed"]
    assert names(evaluate(DEPLOY, ctx(review=None)), False) == ["guardian_approved"]
    assert names(evaluate(DEPLOY, ctx(review=review(GuardianDecision.NEEDS_TESTING))), False) == ["guardian_approved"]
    assert names(evaluate(DEPLOY, ctx(review=review(risk=45))), False) == ["risk_below_threshold"]
    assert evaluate(DEPLOY, ctx(review=review(risk=45), risk_threshold=50)).status is ActionStatus.awaiting_approval


def test_blocklist_and_cooldown():
    assert names(evaluate(DEPLOY, ctx(blocklist=["deploy_patch"])), False) == ["not_blocked_or_cooling_down"]
    recent = Deployment(id="DEP-0001", ticket_id="INC-0000", action=ActionName.set_config, args={}, status=DeploymentStatus.succeeded,
                        started_at=NOW - timedelta(seconds=30))
    assert names(evaluate(DEPLOY, ctx(last_deployment=recent, cooldown_s=120)), False) == ["not_blocked_or_cooling_down"]
    assert evaluate(DEPLOY, ctx(last_deployment=recent, cooldown_s=10)).status is ActionStatus.awaiting_approval
    rollback = ActionRequest(action=ActionName.rollback, args={"deployment_id": "DEP-0001"}, ticket_id="INC-0001")
    decision = evaluate(rollback, ctx(last_deployment=recent, sandbox_run=None, review=None))
    assert decision.status is ActionStatus.awaiting_approval  # rollback needs no sandbox/review and ignores cooldown


def test_config_values_are_checked_against_the_key_specific_range():
    def set_config(key, value):
        return ActionRequest(action=ActionName.set_config, args={"service": "orders-api", "key": key, "value": value}, ticket_id="INC-0001")

    assert evaluate(set_config("DB_POOL_MAX", 20), ctx()).checks[2].ok is True   # a pool size, not a timeout
    assert evaluate(set_config("DB_TIMEOUT_MS", 3000), ctx()).checks[2].ok is True
    denied = evaluate(set_config("DB_POOL_MAX", 1), ctx())
    assert denied.checks[2].ok is False and "below the minimum 2" in denied.checks[2].detail
    denied = evaluate(set_config("DB_TIMEOUT_MS", 50), ctx())
    assert denied.checks[2].ok is False and "below the minimum 100" in denied.checks[2].detail
    denied = evaluate(set_config("DB_POOL_MAX", 500), ctx())
    assert denied.checks[2].ok is False and "above the maximum 100" in denied.checks[2].detail
