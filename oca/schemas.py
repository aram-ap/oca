"""Shared data contracts for the OCA platform.

This module imports nothing else from ``oca`` so that every other module (and the
agent package's tests) can depend on it without cycles. The agent output
contracts (Diagnosis, FixProposal, Review) must stay field-for-field identical
to the JSON schemas in ``models/core/state.py``; ``tests/test_contracts.py``
enforces that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums


class TicketStatus(str, Enum):
    detected = "detected"
    investigating = "investigating"
    fix_proposed = "fix_proposed"
    sandbox_validated = "sandbox_validated"
    reviewed = "reviewed"
    gate = "gate"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    deploying = "deploying"
    awaiting_verification = "awaiting_verification"  # deployed and improved, but SLOs still breached: a human confirms
    resolved = "resolved"
    denied = "denied"
    rolled_back = "rolled_back"
    failed = "failed"


TERMINAL_STATUSES = frozenset({
    TicketStatus.resolved, TicketStatus.rolled_back, TicketStatus.failed,
})
# denied is not terminal: a human may override and approve the pending action.
OPEN_STATUSES = frozenset(set(TicketStatus) - TERMINAL_STATUSES)


class TicketSource(str, Enum):
    health_check = "health_check"
    jira = "jira"
    slack = "slack"
    manual = "manual"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Actor(str, Enum):
    health_check = "health_check"
    jira = "jira"
    slack = "slack"
    investigator = "investigator"
    engineer = "engineer"
    guardian = "guardian"
    orchestrator = "orchestrator"
    gate = "gate"
    executor = "executor"
    human = "human"
    system = "system"


class EventType(str, Enum):
    detected = "detected"
    reported = "reported"
    check_failed = "check_failed"
    finding = "finding"
    diagnosis = "diagnosis"
    patch = "patch"
    sandbox_result = "sandbox_result"
    review = "review"
    action_request = "action_request"
    gate_decision = "gate_decision"
    human_decision = "human_decision"
    run = "run"
    resolved = "resolved"
    rolled_back = "rolled_back"
    status_change = "status_change"
    error = "error"
    external_sync = "external_sync"
    report = "report"


class GuardianDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_TESTING = "NEEDS_TESTING"


class ActionName(str, Enum):
    deploy_patch = "deploy_patch"
    set_config = "set_config"
    restart_service = "restart_service"
    rollback = "rollback"


class ActionStatus(str, Enum):
    denied = "denied"
    policy_pass = "policy_pass"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    executed = "executed"
    failed = "failed"


class DeploymentStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"


class OutboxStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class OutboundDetail(str, Enum):
    minimal = "minimal"
    summary = "summary"
    off = "off"


class Role(str, Enum):
    investigator = "investigator"
    engineer = "engineer"
    guardian = "guardian"
    orchestrator = "orchestrator"
    health = "health"
    jira = "jira"
    slack = "slack"
    admin = "admin"


# ------------------------------------------------------------------- health checks


class CheckResult(BaseModel):
    ok: bool
    check: str
    service: str
    detail: str
    metrics: dict[str, float] = Field(default_factory=dict)
    breaches: list[str] = Field(default_factory=list)
    ts: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


class HealthCheckInfo(BaseModel):
    name: str
    service: str
    enabled: bool
    interval_s: int
    opens_ticket: bool = True
    last_run: datetime | None = None
    last_ok: bool | None = None
    last_result: CheckResult | None = None


# -------------------------------------------------------------------------- tickets


class TicketCreate(BaseModel):
    source: TicketSource
    service: str
    title: str
    severity: Severity = Severity.medium
    description: str | None = None
    external_ref: dict[str, Any] | None = None
    metrics_before: dict[str, Any] | None = None
    metrics_at_detect: dict[str, Any] | None = None


class Ticket(BaseModel):
    id: str
    source: TicketSource
    service: str
    title: str
    status: TicketStatus
    severity: Severity
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    metrics_before: dict[str, Any] | None = None
    metrics_at_detect: dict[str, Any] | None = None
    external_ref: dict[str, Any] | None = None
    attempt: int = 0
    current_deployment_id: str | None = None
    pending_action_id: int | None = None


class TicketEventCreate(BaseModel):
    actor: Actor
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class TicketEvent(TicketEventCreate):
    id: int
    ticket_id: str
    ts: datetime


class TicketWithEvents(Ticket):
    events: list[TicketEvent] = Field(default_factory=list)


class MTTR(BaseModel):
    detection_s: float | None = None
    diagnosis_s: float | None = None
    fix_s: float | None = None
    validation_s: float | None = None
    deployment_s: float | None = None
    total_s: float | None = None


# ------------------------------------------------------------------- gate / actions


class Evidence(BaseModel):
    sandbox_run_id: str | None = None
    risk_score: int | None = None
    guardian_decision: GuardianDecision | None = None
    tests_passed: bool | None = None
    diagnosis_confidence: float | None = None


class ActionRequest(BaseModel):
    action: ActionName
    args: dict[str, Any]
    ticket_id: str
    requested_by: Actor = Actor.orchestrator
    evidence: Evidence = Field(default_factory=Evidence)


class PolicyCheck(BaseModel):
    n: int
    name: str
    ok: bool | None  # None = not evaluated because an earlier check failed
    detail: str


class GateDecision(BaseModel):
    action_id: int | None = None
    ticket_id: str
    action: ActionName
    args: dict[str, Any]
    status: ActionStatus
    checks: list[PolicyCheck]
    reason: str
    requires_human: bool
    ts: datetime = Field(default_factory=utcnow)


class Action(BaseModel):
    id: int
    ticket_id: str
    action: ActionName
    args: dict[str, Any]
    requested_by: Actor
    evidence: Evidence
    status: ActionStatus
    checks: list[PolicyCheck]
    reason: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    deployment_id: str | None = None


# ---------------------------------------------------------------------- deployments


class Deployment(BaseModel):
    id: str
    ticket_id: str | None
    action: ActionName
    args: dict[str, Any]
    backup_ref: str | None = None
    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    git_sha: str | None = None
    prev_sha: str | None = None
    driftwood_deploy_id: int | None = None
    note: str | None = None


# -------------------------------------------------------------------------- sandbox


class SandboxMetrics(BaseModel):
    """Shape of the company load generator's JSON summary, measured by OCA."""

    requests: int
    failures: int
    error_rate_pct: float
    rps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float
    p99_ms: float = 0.0
    statuses: dict[str, int] = Field(default_factory=dict)
    by_endpoint: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Read from the sandbox's own Prometheus after the run, so the sandbox judges by the same
    # SLO set as the production detector (a saturated pool with 0% errors is still an incident).
    db_pool_utilization_pct: float | None = None
    db_errors_per_s: float | None = None
    rate: int | None = None  # target requests/s the generator was asked for


class TestRunResult(BaseModel):
    __test__ = False  # not a pytest test class

    passed: bool
    total: int = 0
    failed: int = 0
    duration_s: float = 0.0
    output_tail: str = ""
    command: str = ""


class SandboxRun(BaseModel):
    id: str
    ticket_id: str
    patch_ref: str | None = None
    base_sha: str | None = None
    before: SandboxMetrics | None = None
    after: SandboxMetrics | None = None
    tests: TestRunResult | None = None
    baseline: dict[str, float] = Field(default_factory=dict)
    reproduced: bool | None = None
    passed: bool | None = None
    created_at: datetime
    updated_at: datetime
    notes: str = ""
    production: dict[str, Any] | None = None  # not stored: prepare() attaches the live production snapshot


class RiskFlags(BaseModel):
    """Deterministic facts about a diff, computed by the server for the Guardian and dashboard."""

    files_changed: int = 0
    lines_changed: int = 0
    db_migration: bool = False
    auth_affected: bool = False
    public_api_affected: bool = False
    deps_changed: bool = False
    config_only: bool = False


# ---------------------------------------------- agent output contracts (mirror models/)


class Diagnosis(BaseModel):
    """Investigator output. Mirrors models.core.state.DIAGNOSIS_SCHEMA."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str = Field(min_length=1)
    evidence_refs: list[str]
    confidence: float = Field(ge=0, le=1)
    unresolved_questions: list[str]


class FixProposal(BaseModel):
    """Engineer output. Mirrors models.core.state.PATCH_SCHEMA."""

    model_config = ConfigDict(extra="forbid")

    patch_ref: str = Field(min_length=1)
    diff: str = Field(min_length=1)
    evidence_refs: list[str]
    reproduced: bool
    passed: bool
    test_commands: list[str]
    before: dict[str, Any]
    after: dict[str, Any]


class Review(BaseModel):
    """Guardian output. Mirrors models.core.state.REVIEW_SCHEMA. APPROVE is advisory."""

    model_config = ConfigDict(extra="forbid")

    risk_score: int = Field(ge=0, le=100)
    policy_concerns: list[str]
    decision: GuardianDecision
    justification: str = Field(min_length=1)
    evidence_refs: list[str]


# --------------------------------------------------------------------- integrations


class InboundTicket(BaseModel):
    system: str
    key: str
    url: str | None = None
    title: str
    description: str | None = None
    severity: Severity = Severity.medium
    service: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class OutboundMessage(BaseModel):
    target: str
    kind: str
    payload: dict[str, Any]
    ticket_id: str


class IntegrationStatus(BaseModel):
    name: str
    configured: bool
    connected: bool = False
    outbox_pending: int = 0
    outbox_failed: int = 0
    bytes_sent: int = 0
    last_sync: datetime | None = None
    outbound_detail: OutboundDetail = OutboundDetail.off


# ------------------------------------------------------------------------- dashboard


class LocalAIStats(BaseModel):
    hardware: str
    model: str
    inference: str = "100% LOCAL"
    cloud_requests: int = 0
    bytes_uploaded: int = 0
    ticketing_mode: str = "not connected · 0 bytes external"
    ticketing_bytes_sent: int = 0
    ollama_url: str
    loaded: bool | None = None
