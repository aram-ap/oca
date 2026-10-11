"""Investigator -> Engineer -> Guardian handoffs, retries, and the final ActionRequest (D2, D6).

The orchestrator talks to the Main Process only over HTTP with the orchestrator token
(``models.tools.client.PlatformClient``), so the same code runs in-process inside
``oca`` and standalone via ``python3 -m models.main --ticket INC-0042``. Guardian
approval is advisory: this module builds the ActionRequest, the deterministic gate
(and by default a human) decides whether anything reaches production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from models.core.agent import AgentError
from models.core.llm import OllamaError
from models.core.state import DIAGNOSIS_SCHEMA, PATCH_SCHEMA, REVIEW_SCHEMA, AgentResult, Observation
from models.core.tool_registry import ToolError, validate
from models.tools.client import ToolAPIError

log = logging.getLogger("oca.orchestrator")

CONFIG_KEYS = ("DB_TIMEOUT_MS", "DB_POOL_MAX", "DB_POOL_ACQUIRE_TIMEOUT_MS")
SERVER_SIDE_TOOLS = frozenset({"apply_patch", "set_config", "create_migration", "run_tests", "run_loadtest"})  # server-logged
ROLES = ("investigator", "engineer", "guardian")
OUTPUT_SCHEMAS = {"investigator": DIAGNOSIS_SCHEMA, "engineer": PATCH_SCHEMA, "guardian": REVIEW_SCHEMA}
REVIEW_EVIDENCE_TOOLS = ("get_diff", "get_test_results", "get_sandbox_results")
# Tickets in these states are owned by the gate, a human, or the executor; agents must not run again.
NO_REPLAY_STATUSES = frozenset({"awaiting_approval", "approved", "deploying", "resolved", "rolled_back"})


class StageFailure(Exception):
    """A stage produced no usable result; the current attempt is spent."""

    def __init__(self, message: str, feedback: str | None = None):
        super().__init__(message)
        self.feedback = feedback  # what the next attempt's agents should know about this failure


def failure_feedback(role: str, exc: AgentError) -> str:
    """Summarise a spent attempt for the next one: which tools ran, how often, and why writes failed."""
    counts: dict[str, int] = {}
    errors: list[str] = []
    for o in exc.observations:
        counts[o.tool] = counts.get(o.tool, 0) + 1
        if isinstance(o.result, dict) and (o.result.get("applied") is False or not o.ok):
            detail = o.result.get("error") or ""
            if detail and o.tool in ("apply_patch", "set_config", "create_migration"):
                errors.append(f"{o.tool}: {detail[:200]}")
    used = ", ".join(f"{t} x{n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "no tools"
    text = f"Previous attempt: {role} failed ({exc}). Tools used: {used}."
    if errors:
        text += " Write failures: " + " | ".join(dict.fromkeys(errors[-3:]))
    if role == "engineer":
        text += (" This time: reproduce once with run_loadtest, then fix with set_config (config values) or "
                 "create_migration (indexes); do not hand-write a diff unless neither applies, and never repeat a call.")
    return text


class PlatformError(RuntimeError):
    """The Main Process itself is unreachable or rejected a platform call."""


@dataclass
class PipelineResult:
    ticket_id: str
    status: str  # the gate's status (awaiting_approval | policy_pass | denied) or "failed"
    attempts: int
    reason: str = ""
    action_request: dict[str, Any] | None = None
    gate_response: dict[str, Any] | None = None

    @property
    def submitted(self) -> bool:
        return self.gate_response is not None


def config_change(diff: str) -> tuple[str, int] | None:
    """If the diff only changes one allowlisted key in an .env file, return (key, value)."""
    files = [m.group(1) for m in re.finditer(r"^\+\+\+ (?:b/)?(\S+)", diff, re.M) if m.group(1) != "/dev/null"]
    if not files or not all(f.endswith(".env") for f in files):
        return None
    changes = re.findall(r"^\+([A-Z0-9_]+)=(-?\d+)\s*$", diff, re.M)
    if len(changes) != 1 or changes[0][0] not in CONFIG_KEYS:
        return None
    return changes[0][0], int(changes[0][1])


def _trim(value: Any, limit: int = 600) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "..."


def observation_payload(role: str, observation: Observation) -> dict[str, Any]:
    result = observation.result
    evidence_ref = result.get("evidence_ref") if isinstance(result, dict) else None
    summary = result.get("error") if isinstance(result, dict) and not observation.ok else None
    if summary is None and isinstance(result, dict):
        summary = {k: v for k, v in result.items() if k not in ("entries", "diff", "content", "series", "containers",
                                                                 "commits", "deployments", "by_endpoint")}
    return {"role": role, "tool": observation.tool, "arguments": _trim(observation.arguments, 400), "ok": observation.ok,
            "evidence_ref": evidence_ref, "summary": _trim(summary, 800)}


def result_payload(result: AgentResult, key: str, **extra: Any) -> dict[str, Any]:
    return {key: result.output, "steps": result.steps,
            "observations": [{"tool": o.tool, "ok": o.ok, "evidence_ref": o.result.get("evidence_ref") if isinstance(o.result, dict) else None}
                             for o in result.observations], **extra}


class Orchestrator:
    def __init__(self, platform, agents: dict[str, Any] | None = None, *, max_attempts: int = 2,
                 timeout_s: float = 4 * 3600) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.platform = platform
        self.agents = agents or {}
        self.max_attempts = max(1, int(max_attempts))
        self.timeout_s = timeout_s
        self.ticket_id: str | None = None
        self._active: set[str] = set()
        self._attempt = 0

    # ------------------------------------------------------------- platform I/O

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            return await self.platform.request(method, path, body)
        except ToolAPIError as exc:
            if exc.status == 409:
                raise StageFailure(f"{path}: {exc.body[:300] or 'conflict'}") from exc
            if exc.status == 502:  # the platform's runner failed (docker/git/ops script); this attempt is spent
                raise StageFailure(f"{path}: {exc.body[:500] or 'runner failure'}") from exc
            raise PlatformError(f"{method} {path}: {exc} {exc.body[:300]}".strip()) from exc

    async def _ticket(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/api/tickets/{ticket_id}")

    async def _status(self, ticket_id: str, status: str, note: str | None = None, attempt: int | None = None) -> None:
        body: dict[str, Any] = {"status": status}
        if note:
            body["note"] = note[:500]
        if attempt is not None:
            body["attempt"] = attempt
        await self._call("POST", f"/api/tickets/{ticket_id}/status", body)

    async def _event(self, ticket_id: str, type: str, payload: dict[str, Any], actor: str = "orchestrator") -> None:
        await self._call("POST", f"/api/tickets/{ticket_id}/events", {"type": type, "payload": payload, "actor": actor})

    async def observer(self, role: str, observation: Observation) -> None:
        """Bound to every agent as ``on_observation`` so tool use streams onto the timeline."""
        if self.ticket_id is None or observation.tool in SERVER_SIDE_TOOLS:
            return
        try:
            await self._event(self.ticket_id, "finding", observation_payload(role, observation), actor=role)
        except (PlatformError, StageFailure) as exc:
            log.warning("could not record observation: %s", exc)

    # ------------------------------------------------------------------ agents

    async def _agent(self, role: str, ticket_id: str, context: dict[str, Any]) -> AgentResult:
        agent = self.agents.get(role)
        if agent is None:
            raise PlatformError(f"no {role} agent configured")
        try:
            result = await agent.run(context)
            validate(result.output, OUTPUT_SCHEMAS[role], role)  # defence in depth; the loop validates too
            return result
        except (AgentError, OllamaError, ToolAPIError, ToolError) as exc:
            await self._event(ticket_id, "error", {"where": role, "error": f"{type(exc).__name__}: {exc}"[:800]})
            hint = failure_feedback(role, exc) if isinstance(exc, AgentError) else None
            raise StageFailure(f"{role} failed: {exc}", feedback=hint) from exc

    @staticmethod
    def audit_review(review: AgentResult, run: dict[str, Any]) -> None:
        """The Guardian must have fetched server evidence for *this* run and patch before approving."""
        seen: dict[str, dict[str, Any]] = {}
        for o in review.observations:
            if o.ok and o.tool in REVIEW_EVIDENCE_TOOLS and isinstance(o.result, dict):
                seen[o.tool] = o.result
        missing = [t for t in REVIEW_EVIDENCE_TOOLS if t not in seen]
        if missing:
            raise StageFailure(f"Guardian approved without consulting {', '.join(missing)}")
        run_id, patch_ref = run.get("id"), run.get("patch_ref")
        diff, tests, sandbox = seen["get_diff"], seen["get_test_results"], seen["get_sandbox_results"]
        problems = []
        if diff.get("sandbox_run_id") != run_id or diff.get("patch_ref") != patch_ref:
            problems.append("diff evidence is for a different run or patch")
        if tests.get("sandbox_run_id") != run_id or not (tests.get("tests") or {}).get("passed"):
            problems.append("test evidence is missing, failed, or for a different run")
        if (sandbox.get("id") != run_id or sandbox.get("patch_ref") != patch_ref
                or sandbox.get("reproduced") is not True or sandbox.get("passed") is not True):
            problems.append("sandbox evidence is missing, failed, or for a different run")
        if problems:
            raise StageFailure("Guardian evidence audit failed: " + "; ".join(problems))

    @staticmethod
    def base_context(ticket: dict[str, Any]) -> dict[str, Any]:
        detected = next((e for e in ticket.get("events", []) if e.get("type") in ("detected", "reported")), {})
        payload = detected.get("payload", {}) if detected else {}
        status = payload.get("status") or {}
        return {
            "ticket_id": ticket["id"], "service": ticket["service"], "title": ticket["title"],
            "severity": ticket.get("severity"), "source": ticket.get("source"),
            "description": payload.get("description"),
            "metrics_at_detect": ticket.get("metrics_at_detect"), "metrics_before": ticket.get("metrics_before"),
            "detected": {"check": payload.get("check"), "detail": payload.get("detail"), "breaches": payload.get("breaches"),
                         "version": status.get("version"), "git_sha": status.get("git_sha"),
                         "db_errors": status.get("db_errors"), "slowest_queries_p95_ms": status.get("slowest_queries_p95_ms")},
            "fix_branch": f"fix/{ticket['id']}",
        }

    @staticmethod
    def sandbox_summary(run: dict[str, Any]) -> dict[str, Any]:
        def metrics(m: dict | None) -> dict | None:
            return None if not m else {k: m.get(k) for k in ("requests", "failures", "error_rate_pct", "p50_ms", "p95_ms", "p99_ms", "by_endpoint")}
        tests = run.get("tests") or None
        return {"sandbox_run_id": run.get("id"), "base_sha": run.get("base_sha"), "patch_ref": run.get("patch_ref"),
                "reproduced": run.get("reproduced"), "passed": run.get("passed"), "baseline": run.get("baseline"),
                "before": metrics(run.get("before")), "after": metrics(run.get("after")),
                "tests": None if not tests else {k: tests.get(k) for k in ("passed", "total", "failed", "output_tail")}}

    # -------------------------------------------------------------------- flow

    async def run_ticket(self, ticket_id: str) -> PipelineResult:
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError("A ticket id is required")
        if ticket_id in self._active:
            raise RuntimeError(f"{ticket_id} is already running in this orchestrator")
        self._active.add(ticket_id)
        self.ticket_id, self._attempt = ticket_id, 0
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._run(ticket_id)
        except TimeoutError:
            reason = f"orchestrator deadline of {self.timeout_s:.0f}s exceeded"
            log.error("%s: %s", ticket_id, reason)
            try:
                await self._event(ticket_id, "error", {"where": "orchestrator", "error": reason})
                await self._status(ticket_id, "failed", note=reason)
            except (PlatformError, StageFailure):
                pass
            return PipelineResult(ticket_id, "failed", self._attempt, reason)
        finally:
            self._active.discard(ticket_id)
            self.ticket_id = None

    async def _run(self, ticket_id: str) -> PipelineResult:
        ticket = await self._ticket(ticket_id)
        if not isinstance(ticket, dict) or ticket.get("id") != ticket_id:
            raise PlatformError(f"platform did not return ticket {ticket_id}")
        if ticket.get("status") in NO_REPLAY_STATUSES:
            raise ValueError(f"{ticket_id} is {ticket['status']}; the agents must not run again on it")
        base = self.base_context(ticket)
        feedback: list[str] = []
        failures: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            self._attempt = attempt
            try:
                await self._status(ticket_id, "investigating", attempt=attempt, note=f"attempt {attempt}/{self.max_attempts}")
                diag = await self._agent("investigator", ticket_id, {**base, "attempt": attempt, "feedback": list(feedback)})
                await self._event(ticket_id, "diagnosis", result_payload(diag, "diagnosis", attempt=attempt), actor="investigator")

                run = await self._call("POST", f"/api/sandbox/{ticket_id}/prepare")
                if not run.get("reproduced"):
                    feedback.append("The sandbox at the production sha did not reproduce the incident under load; "
                                    "re-examine the diagnosis and the detection evidence.")
                    raise StageFailure("incident not reproduced in sandbox")

                await self._status(ticket_id, "fix_proposed")
                fix, run = await self._engineer_cycle(ticket_id, base, diag.output, run, feedback)
                await self._status(ticket_id, "sandbox_validated")

                await self._status(ticket_id, "reviewed")
                review = await self._review(ticket_id, base, diag.output, fix.output, run, feedback)
                if review.output["decision"] == "NEEDS_TESTING":
                    feedback.append("Guardian requested more testing: " + review.output["justification"] + " Concerns: "
                                    + "; ".join(review.output.get("policy_concerns", [])))
                    fix, run = await self._engineer_cycle(ticket_id, base, diag.output, run, feedback)
                    review = await self._review(ticket_id, base, diag.output, fix.output, run, feedback)
                if review.output["decision"] != "APPROVE":
                    feedback.append(f"Guardian {review.output['decision']}: {review.output['justification']} Concerns: "
                                    + "; ".join(review.output.get("policy_concerns", [])))
                    raise StageFailure(f"Guardian decision {review.output['decision']}")

                # An APPROVE only counts if the Guardian actually looked at this run's server evidence.
                try:
                    self.audit_review(review, run)
                except StageFailure as exc:
                    await self._event(ticket_id, "error", {"where": "guardian", "error": str(exc)})
                    feedback.append(f"{exc}. Fetch get_diff, get_test_results and get_sandbox_results for "
                                    f"{run.get('id')} before deciding.")
                    review = await self._review(ticket_id, base, diag.output, fix.output, run, feedback)
                    if review.output["decision"] != "APPROVE":
                        raise StageFailure(f"Guardian decision {review.output['decision']} after evidence audit")
                    self.audit_review(review, run)

                request = self.action_request(ticket_id, base["service"], fix.output, run, review.output, diag.output)
                decision = await self._call("POST", "/api/actions", request)
                if not isinstance(decision, dict):
                    raise PlatformError("gate returned no decision; submission outcome is uncertain, not retrying")
                log.info("%s: gate -> %s (%s)", ticket_id, decision.get("status"), decision.get("reason"))
                return PipelineResult(ticket_id, str(decision.get("status")), attempt, str(decision.get("reason", "")),
                                      action_request=request, gate_response=decision)
            except StageFailure as exc:
                failures.append(f"attempt {attempt}: {exc}")
                if exc.feedback:
                    feedback.append(exc.feedback)
                log.warning("%s attempt %d failed: %s", ticket_id, attempt, exc)
        summary = "attempts exhausted: " + " | ".join(failures)
        await self._event(ticket_id, "error", {"where": "orchestrator", "error": summary[:800], "attempts": failures})
        await self._status(ticket_id, "failed", note=summary)
        try:
            await self._call("POST", f"/api/sandbox/{ticket_id}/teardown")
        except (PlatformError, StageFailure):
            pass
        return PipelineResult(ticket_id, "failed", self.max_attempts, summary)

    async def _engineer_cycle(self, ticket_id: str, base: dict, diagnosis: dict, run: dict, feedback: list[str]):
        """Engineer patches in the sandbox; the server validates. One retry with the failure as feedback."""
        for round_ in (1, 2):
            context = {**base, "diagnosis": diagnosis, "sandbox": self.sandbox_summary(run), "feedback": list(feedback),
                       "instructions": ("Patches are applied with apply_patch on branch " + base["fix_branch"]
                                        + " inside the sandbox worktree and are cumulative. Reproduce first, then fix, then test.")}
            fix = await self._agent("engineer", ticket_id, context)
            await self._event(ticket_id, "patch", result_payload(fix, "patch", round=round_), actor="engineer")
            try:
                run = await self._call("POST", f"/api/sandbox/{ticket_id}/validate")
            except StageFailure as exc:
                feedback.append(f"Validation could not run: {exc}. You must apply a patch with apply_patch before finishing.")
                if round_ == 2:
                    raise
                continue
            if run.get("passed"):
                return fix, run
            after, tests = run.get("after") or {}, run.get("tests") or {}
            feedback.append(f"Sandbox validation failed on {run.get('patch_ref')}: error rate {after.get('error_rate_pct')}%, "
                            f"p95 {after.get('p95_ms')} ms, tests passed={tests.get('passed')} ({tests.get('failed', '?')} failed). "
                            + (tests.get("output_tail") or "")[-400:])
        raise StageFailure("fix not proven in sandbox after two engineer rounds")

    async def _review(self, ticket_id: str, base: dict, diagnosis: dict, fix: dict, run: dict,
                      feedback: list[str] | None = None) -> AgentResult:
        context = {**base, "diagnosis": diagnosis, "patch": {k: fix.get(k) for k in ("patch_ref", "evidence_refs", "test_commands")},
                   "sandbox": self.sandbox_summary(run), "feedback": list(feedback or []),
                   "instructions": ("Fetch get_diff, get_test_results and get_sandbox_results for this ticket before deciding; "
                                    "an APPROVE without that server-measured evidence is rejected.")}
        review = await self._agent("guardian", ticket_id, context)
        await self._event(ticket_id, "review", result_payload(review, "review"), actor="guardian")
        return review

    @staticmethod
    def action_request(ticket_id: str, service: str, fix: dict, run: dict, review: dict, diagnosis: dict) -> dict[str, Any]:
        change = config_change(fix.get("diff", ""))
        if change is not None:
            action, args = "set_config", {"service": service, "key": change[0], "value": change[1]}
        else:
            action, args = "deploy_patch", {"ticket_id": ticket_id, "branch": run.get("patch_ref") or f"fix/{ticket_id}"}
        tests = run.get("tests") or {}
        return {"action": action, "args": args, "ticket_id": ticket_id, "requested_by": "orchestrator",
                "evidence": {"sandbox_run_id": run.get("id"), "risk_score": review.get("risk_score"),
                             "guardian_decision": review.get("decision"), "tests_passed": tests.get("passed"),
                             "diagnosis_confidence": diagnosis.get("confidence")}}
