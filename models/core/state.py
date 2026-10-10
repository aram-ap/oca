"""Per-run state and structured role outputs (platform schemas remain planned)."""

from dataclasses import dataclass, field
from typing import Any

from models.core.tool_registry import object_schema

TEXT = {"type": "string", "minLength": 1}
TEXTS = {"type": "array", "items": TEXT}
METRICS = {"type": "object"}

DIAGNOSIS_SCHEMA = object_schema({
    "diagnosis": TEXT, "evidence_refs": TEXTS,
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "unresolved_questions": TEXTS,
})
PATCH_SCHEMA = object_schema({
    "patch_ref": TEXT, "diff": TEXT, "evidence_refs": TEXTS,
    "reproduced": {"type": "boolean"}, "passed": {"type": "boolean"},
    "test_commands": TEXTS, "before": METRICS, "after": METRICS,
})
REVIEW_SCHEMA = object_schema({
    "risk_score": {"type": "integer", "minimum": 0, "maximum": 100},
    "policy_concerns": TEXTS,
    "decision": {"type": "string", "enum": ["APPROVE", "REJECT", "NEEDS_TESTING"]},
    "justification": TEXT, "evidence_refs": TEXTS,
})


@dataclass
class Observation:
    tool: str
    arguments: dict[str, Any]
    result: Any
    ok: bool


@dataclass
class AgentResult:
    role: str
    output: dict[str, Any]
    steps: int
    observations: list[Observation] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
