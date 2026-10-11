"""Command check gate: deterministic policy over an ActionRequest. No LLM, no side effects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from oca.schemas import (
    ActionRequest, ActionStatus, Deployment, GateDecision, GuardianDecision, PolicyCheck, Review,
    SandboxRun,
)

CHECK_NAMES = ["action_allowlisted", "args_match_schema", "args_within_values", "sandbox_reproduced_and_fixed",
               "tests_passed", "guardian_approved", "risk_below_threshold", "not_blocked_or_cooling_down"]


class ArgSpec(BaseModel):
    type: Literal["str", "int"] | None = None
    enum: list[Any] | None = None
    min: int | None = None
    max: int | None = None
    pattern: str | None = None


class ActionSpec(BaseModel):
    description: str = ""
    args: dict[str, ArgSpec] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)


class Allowlist(BaseModel):
    version: int = 1
    defaults: dict[str, int] = Field(default_factory=dict)
    actions: dict[str, ActionSpec]


def load_allowlist(path: str | Path) -> Allowlist:
    return Allowlist.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass
class GateContext:
    allowlist: Allowlist
    sandbox_run: SandboxRun | None = None
    review: Review | None = None
    last_deployment: Deployment | None = None
    require_human_approval: bool = False
    risk_threshold: int = 30
    cooldown_s: int = 120
    blocklist: list[str] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _check_value(name: str, value: Any, spec: ArgSpec) -> str | None:
    if spec.enum is not None and value not in spec.enum:
        return f"{name}={value!r} is not one of {spec.enum}"
    if spec.type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{name} must be an integer"
        if spec.min is not None and value < spec.min:
            return f"{name}={value} is below the minimum {spec.min}"
        if spec.max is not None and value > spec.max:
            return f"{name}={value} is above the maximum {spec.max}"
    if spec.type == "str":
        if not isinstance(value, str):
            return f"{name} must be a string"
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            return f"{name}={value!r} does not match {spec.pattern}"
    return None


def evaluate(request: ActionRequest, ctx: GateContext) -> GateDecision:
    """Run the eight policy checks in README order; stop at the first failure."""
    checks: list[PolicyCheck] = []
    failed: str | None = None

    def record(n: int, ok: bool | None, detail: str) -> None:
        nonlocal failed
        checks.append(PolicyCheck(n=n, name=CHECK_NAMES[n - 1], ok=ok, detail=detail))
        if ok is False and failed is None:
            failed = f"check {n} ({CHECK_NAMES[n - 1]}): {detail}"

    spec = ctx.allowlist.actions.get(request.action.value)
    record(1, spec is not None, f"{request.action.value} is allowlisted" if spec else f"{request.action.value} is not an allowlisted action")
    required = set(spec.requires) if spec else set()

    if spec is not None:
        expected, got = set(spec.args), set(request.args)
        if expected == got:
            record(2, True, f"args {sorted(got)} match the schema")
        else:
            record(2, False, f"expected args {sorted(expected)}, got {sorted(got)}")
    else:
        record(2, None, "skipped")

    if spec is not None and checks[-1].ok:
        problems = [p for name, s in spec.args.items() if (p := _check_value(name, request.args[name], s))]
        record(3, not problems, "; ".join(problems) if problems else "all values within allowed ranges")
    else:
        record(3, None, "skipped")

    run = ctx.sandbox_run
    if {"sandbox_reproduced", "sandbox_fixed"} & required:
        if failed:
            record(4, None, "skipped")
        elif run is None:
            record(4, False, "no sandbox run recorded for this ticket")
        elif run.reproduced is not True:
            record(4, False, f"incident not reproduced in sandbox ({run.id})")
        elif run.passed is not True:
            record(4, False, f"fix not proven in sandbox ({run.id})")
        else:
            record(4, True, f"{run.id}: reproduced and fixed "
                            f"(error rate {run.before.error_rate_pct if run.before else '?'}% -> {run.after.error_rate_pct if run.after else '?'}%)")
    else:
        record(4, True, "not required for this action")

    if "tests_pass" in required:
        if failed:
            record(5, None, "skipped")
        elif run is None or run.tests is None:
            record(5, False, "no sandbox test results")
        else:
            record(5, run.tests.passed, f"{run.tests.total - run.tests.failed}/{run.tests.total} tests passed" if run.tests.passed
                   else f"{run.tests.failed} test(s) failed")
    else:
        record(5, True, "not required for this action")

    review = ctx.review
    if "guardian_approve" in required:
        if failed:
            record(6, None, "skipped")
        elif review is None:
            record(6, False, "no Guardian review recorded")
        else:
            record(6, review.decision is GuardianDecision.APPROVE, f"Guardian decision {review.decision.value}")
    else:
        record(6, True, "not required for this action")

    if "risk_below" in required:
        if failed:
            record(7, None, "skipped")
        elif review is None:
            record(7, False, "no risk score available")
        else:
            record(7, review.risk_score < ctx.risk_threshold, f"risk {review.risk_score}/100 vs threshold {ctx.risk_threshold}")
    else:
        record(7, True, "not required for this action")

    if failed:
        record(8, None, "skipped")
    elif request.action.value in ctx.blocklist:
        record(8, False, f"{request.action.value} is on the blocklist")
    elif (ctx.last_deployment is not None and request.action.value != "rollback" and ctx.cooldown_s > 0
          and ctx.last_deployment.started_at and ctx.now - ctx.last_deployment.started_at < timedelta(seconds=ctx.cooldown_s)):
        remaining = ctx.cooldown_s - int((ctx.now - ctx.last_deployment.started_at).total_seconds())
        record(8, False, f"cooldown: {ctx.last_deployment.id} deployed {remaining}s ago (limit {ctx.cooldown_s}s)")
    else:
        record(8, True, "not blocked; no recent deployment in the cooldown window")

    status = ActionStatus.denied if failed else (ActionStatus.awaiting_approval if ctx.require_human_approval else ActionStatus.policy_pass)
    reason = failed or ("policy passed; awaiting human approval" if ctx.require_human_approval else "policy passed; auto-approved")
    return GateDecision(ticket_id=request.ticket_id, action=request.action, args=request.args, status=status,
                        checks=checks, reason=reason, requires_human=ctx.require_human_approval and not failed)
