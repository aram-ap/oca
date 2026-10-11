"""Sequential incident handoffs and advisory action submission (README D2/D4/D6)."""

import asyncio
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from models.agents import Engineer, Guardian, Investigator
from models.core.agent import AgentError
from models.core.llm import OllamaClient, OllamaError
from models.core.state import DIAGNOSIS_SCHEMA, PATCH_SCHEMA, REVIEW_SCHEMA, AgentResult
from models.core.tool_registry import ToolError, validate
from models.platform_client import PlatformClient
from models.tools.client import ToolAPIError


@dataclass
class PipelineResult:
    ticket_id: str
    status: str  # submitted or escalated; not a production deployment status
    attempts: int
    reason: str = ""
    action_request: dict[str, Any] | None = None
    gate_response: dict[str, Any] | None = None


class Orchestrator:
    def __init__(self, *, platform=None, investigator=None, engineer=None, guardian=None,
                 max_attempts: int = 3, timeout_s: float = 1200):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        self.platform = platform if platform is not None else PlatformClient.from_env()
        # Share one local model client, not role tokens or conversation histories.
        llm = OllamaClient() if any(a is None for a in (investigator, engineer, guardian)) else None
        self.investigator = investigator if investigator is not None else Investigator(llm=llm)
        self.engineer = engineer if engineer is not None else Engineer(llm=llm)
        self.guardian = guardian if guardian is not None else Guardian(llm=llm)
        self.max_attempts, self.timeout_s = max_attempts, timeout_s
        self._active = set()

    async def run(self, ticket_id: str) -> PipelineResult:
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError("A nonempty ticket ID is required")
        if ticket_id in self._active:
            raise AgentError("This ticket is already running in this orchestrator")
        self._active.add(ticket_id)
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._run(ticket_id)
        finally:
            self._active.remove(ticket_id)

    async def _event(self, ticket_id, actor, kind, payload):
        # Persist before proceeding. A failed/uncertain API write is never retried.
        await self.platform.append_event(ticket_id, {
            "actor": actor, "type": kind, "payload": deepcopy(payload),
        })

    async def _phase(self, ticket_id, status, attempt):
        await self._event(ticket_id, "orchestrator", "run", {"status": status, "attempt": attempt})

    async def _agent(self, agent, role, schema, context, kind):
        result = await agent.run(deepcopy(context))
        if not isinstance(result, AgentResult) or result.role != role:
            raise AgentError("Agent returned a result for the wrong role")
        validate(result.output, schema, role)
        await self._event(context["ticket_id"], role, kind, {
            "output": result.output, "steps": result.steps,
            "observations": [asdict(o) for o in result.observations],
        })
        return result

    async def _escalate(self, ticket_id, attempts, reason):
        await self._event(ticket_id, "orchestrator", "run", {
            "phase": "escalated", "requires_human": True, "reason": reason, "attempts": attempts,
        })
        return PipelineResult(ticket_id, "escalated", attempts, reason)

    async def _run(self, ticket_id):
        ticket = await self.platform.get_ticket(ticket_id)
        if not isinstance(ticket, dict) or ticket.get("id") != ticket_id:
            raise ValueError("Platform must return the requested ticket as a JSON object with id")
        # No automatic replay of a ticket already handled by the platform/gate.
        if ticket.get("status") != "detected":
            raise ValueError("Only detected tickets can start a new pipeline; resume requires platform coordination")
        context = {"ticket": deepcopy(ticket), "ticket_id": ticket_id,
                   "service": ticket.get("service"), "title": ticket.get("title")}
        diagnosis = None
        patch = None
        feedback = None
        reason = "Attempt budget exhausted"
        for attempt in range(1, self.max_attempts + 1):
            context["attempt"] = attempt
            if feedback is not None:
                context["feedback"] = feedback
            try:
                if diagnosis is None:
                    await self._phase(ticket_id, "investigating", attempt)
                    diagnosis = await self._agent(self.investigator, "investigator", DIAGNOSIS_SCHEMA,
                                                 context, "diagnosis")
                engineer_context = {**context, "diagnosis": diagnosis.output}
                if patch is not None:
                    engineer_context["previous_patch"] = patch.output
                await self._phase(ticket_id, "fix_proposed", attempt)
                patch = await self._agent(self.engineer, "engineer", PATCH_SCHEMA,
                                          engineer_context, "patch")
                if not patch.output["reproduced"] or not patch.output["passed"]:
                    reason = "Sandbox incident did not reproduce or fix validation failed"
                    feedback = {"reason": reason, "previous_patch": patch.output}
                    diagnosis = None
                    continue
                await self._phase(ticket_id, "sandbox_validated", attempt)
                review = await self._agent(self.guardian, "guardian", REVIEW_SCHEMA,
                                           {**context, "diagnosis": diagnosis.output,
                                            "patch": patch.output}, "review")
                await self._phase(ticket_id, "reviewed", attempt)
            except (AgentError, OllamaError, ToolError) as exc:
                return await self._escalate(ticket_id, attempt, str(exc))
            # Transport failures, including uncertain sandbox writes, propagate to
            # the caller. Do not start another agent or attempt after those errors.
            decision = review.output["decision"]
            feedback = {"review": review.output, "previous_patch": patch.output}
            if decision != "APPROVE":
                reason = f"Guardian returned {decision}: {review.output['justification']}"
                if decision == "REJECT":
                    diagnosis = None
                continue
            try:
                action = self._action(ticket_id, patch, review)
            except AgentError as exc:
                return await self._escalate(ticket_id, attempt, str(exc))
            await self._phase(ticket_id, "gate", attempt)
            await self._event(ticket_id, "orchestrator", "action_request", action)
            response = await self.platform.submit_action(action)
            if not isinstance(response, dict):
                raise ToolAPIError("Invalid gate response; submission outcome is uncertain, do not retry")
            return PipelineResult(ticket_id, "submitted", attempt,
                                  action_request=action, gate_response=response)
        return await self._escalate(ticket_id, self.max_attempts, reason)

    @staticmethod
    def _action(ticket_id, patch, review):
        def evidence(tool):
            for observation in reversed(review.observations):
                if (observation.ok and observation.tool == tool
                        and observation.arguments.get("ticket_id") == ticket_id
                        and isinstance(observation.result, dict)):
                    return observation.result
            raise AgentError(f"Guardian is missing server evidence from {tool}")

        sandbox = evidence("get_sandbox_results")
        tests = evidence("get_test_results")
        diff = evidence("get_diff")
        patch_ref = patch.output["patch_ref"]
        run_id = sandbox.get("sandbox_run_id")
        if (patch_ref == "none" or not isinstance(run_id, str) or not run_id.strip()
                or any(item.get("patch_ref") != patch_ref for item in (sandbox, tests, diff))
                or tests.get("sandbox_run_id") != run_id
                or sandbox.get("reproduced") is not True or sandbox.get("passed") is not True
                or tests.get("passed") is not True or diff.get("diff") != patch.output["diff"]):
            raise AgentError("Server evidence is missing, failed, or does not match the reviewed patch/run")
        return {
            "action": "deploy_patch", "args": {"incident": ticket_id, "patch": patch_ref},
            "requested_by": "orchestrator",
            "evidence": {"sandbox_run_id": run_id, "risk_score": review.output["risk_score"],
                         "guardian_decision": "APPROVE"},
        }
