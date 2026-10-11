"""Investigator -> Engineer -> Guardian handoffs, retries, and the final ActionRequest (D2, D6).

The orchestrator talks to the Main Process only over HTTP with the orchestrator token
(``models.tools.client.PlatformClient``), so the same code runs in-process inside
``oca`` and standalone via ``python3 -m models.main --ticket INC-0042``. Guardian
approval is advisory: this module builds the ActionRequest, the deterministic gate
(and by default a human) decides whether anything reaches production.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from models.core.agent import AgentError
from models.core.llm import OllamaError
from models.core.state import AgentResult, Observation
from models.tools.client import ToolAPIError

log = logging.getLogger("oca.orchestrator")

CONFIG_KEYS = ("DB_TIMEOUT_MS", "DB_POOL_MAX", "DB_POOL_ACQUIRE_TIMEOUT_MS")
SERVER_SIDE_TOOLS = frozenset({"apply_patch", "run_tests", "run_loadtest"})  # the server logs these itself
ROLES = ("investigator", "engineer", "guardian")


class StageFailure(Exception):
    """A stage produced no usable result; the current attempt is spent."""


class PlatformError(RuntimeError):
    """The Main Process itself is unreachable or rejected a platform call."""


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
    def __init__(self, platform, agents: dict[str, Any] | None = None, *, max_attempts: int = 2) -> None:
        self.platform = platform
        self.agents = agents or {}
        self.max_attempts = max(1, int(max_attempts))
        self.ticket_id: str | None = None

    # ------------------------------------------------------------- platform I/O

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            return await self.platform.request(method, path, body)
        except ToolAPIError as exc:
            if exc.status == 409:
                raise StageFailure(f"{path}: {exc.body[:300] or 'conflict'}") from exc
            raise PlatformError(f"{method} {path}: {exc}") from exc

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
            return await agent.run(context)
        except (AgentError, OllamaError, ToolAPIError) as exc:
            await self._event(ticket_id, "error", {"where": role, "error": f"{type(exc).__name__}: {exc}"[:800]})
            raise StageFailure(f"{role} failed: {exc}") from exc

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

    async def run_ticket(self, ticket_id: str) -> str:
        self.ticket_id = ticket_id
        ticket = await self._ticket(ticket_id)
        base = self.base_context(ticket)
        feedback: list[str] = []
        failures: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
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
                review = await self._review(ticket_id, base, diag.output, fix.output, run)
                if review.output["decision"] == "NEEDS_TESTING":
                    feedback.append("Guardian requested more testing: " + review.output["justification"] + " Concerns: "
                                    + "; ".join(review.output.get("policy_concerns", [])))
                    fix, run = await self._engineer_cycle(ticket_id, base, diag.output, run, feedback)
                    review = await self._review(ticket_id, base, diag.output, fix.output, run)
                if review.output["decision"] != "APPROVE":
                    feedback.append(f"Guardian {review.output['decision']}: {review.output['justification']} Concerns: "
                                    + "; ".join(review.output.get("policy_concerns", [])))
                    raise StageFailure(f"Guardian decision {review.output['decision']}")

                request = self.action_request(ticket_id, base["service"], fix.output, run, review.output, diag.output)
                decision = await self._call("POST", "/api/actions", request)
                log.info("%s: gate -> %s (%s)", ticket_id, decision.get("status"), decision.get("reason"))
                return str(decision.get("status"))
            except StageFailure as exc:
                failures.append(f"attempt {attempt}: {exc}")
                log.warning("%s attempt %d failed: %s", ticket_id, attempt, exc)
        summary = "attempts exhausted: " + " | ".join(failures)
        await self._event(ticket_id, "error", {"where": "orchestrator", "error": summary[:800], "attempts": failures})
        await self._status(ticket_id, "failed", note=summary)
        try:
            await self._call("POST", f"/api/sandbox/{ticket_id}/teardown")
        except (PlatformError, StageFailure):
            pass
        return "failed"

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

    async def _review(self, ticket_id: str, base: dict, diagnosis: dict, fix: dict, run: dict) -> AgentResult:
        context = {**base, "diagnosis": diagnosis, "patch": {k: fix.get(k) for k in ("patch_ref", "evidence_refs", "test_commands")},
                   "sandbox": self.sandbox_summary(run),
                   "instructions": "Fetch the diff and server-measured evidence with your tools; do not trust the patch summary."}
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
