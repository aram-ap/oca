"""Simulate the platform to verify incident handoffs and fail-closed submission."""

import asyncio
from copy import deepcopy
import json
import unittest

from models.agents import Engineer, Guardian, Investigator
from models.core.agent import AgentError
from models.core.state import AgentResult, Observation
from models.orchestrator import Orchestrator
from models.tools.client import ToolAPIError

DIAGNOSIS = {"diagnosis": "Bad database timeout", "evidence_refs": ["logs:1"],
             "confidence": 0.9, "unresolved_questions": []}
PATCH = {"patch_ref": "FIX-1", "diff": "unified diff", "evidence_refs": ["SB-1"],
         "reproduced": True, "passed": True, "test_commands": ["pytest"],
         "before": {"error_rate": 0.25}, "after": {"error_rate": 0}}
REVIEW = {"risk_score": 8, "policy_concerns": [], "decision": "APPROVE",
          "justification": "Reviewed passing evidence", "evidence_refs": ["SB-1"]}
EVIDENCE = {
    "get_diff": {"patch_ref": "FIX-1", "diff": "unified diff"},
    "get_test_results": {"patch_ref": "FIX-1", "sandbox_run_id": "SB-1", "passed": True},
    "get_sandbox_results": {"patch_ref": "FIX-1", "sandbox_run_id": "SB-1",
                            "reproduced": True, "passed": True},
}


class SimulatedPlatform:
    def __init__(self):
        self.ticket = {"id": "INC-1", "status": "detected", "service": "orders-api"}
        self.events, self.actions = [], []

    async def get_ticket(self, ticket_id):
        return deepcopy(self.ticket)

    async def append_event(self, ticket_id, event):
        self.events.append(deepcopy(event))
        return {"id": len(self.events)}

    async def submit_action(self, action):
        self.actions.append(deepcopy(action))
        return {"status": "awaiting_approval"}


class SimulatedAgent:
    def __init__(self, role, outputs, *, evidence=None):
        self.role, self.outputs, self.contexts = role, list(outputs), []
        self.evidence = deepcopy(EVIDENCE if evidence is None else evidence)

    async def run(self, context):
        self.contexts.append(deepcopy(context))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        observations = [Observation(name, {"ticket_id": context["ticket_id"]}, data, True)
                        for name, data in self.evidence.items()] if self.role == "guardian" else []
        return AgentResult(self.role, deepcopy(output), 2, observations)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def build(self, *, diagnoses=None, patches=None, reviews=None, evidence=None, **limits):
        self.platform = SimulatedPlatform()
        self.investigator = SimulatedAgent("investigator", diagnoses or [DIAGNOSIS])
        self.engineer = SimulatedAgent("engineer", patches or [PATCH])
        self.guardian = SimulatedAgent("guardian", reviews or [REVIEW], evidence=evidence)
        return Orchestrator(platform=self.platform, investigator=self.investigator,
                            engineer=self.engineer, guardian=self.guardian, **limits)

    async def test_handoffs_audit_and_submission_stop_at_gate(self):
        result = await self.build().run("INC-1")
        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.gate_response["status"], "awaiting_approval")
        self.assertEqual(self.engineer.contexts[0]["diagnosis"], DIAGNOSIS)
        self.assertEqual(self.guardian.contexts[0]["patch"], PATCH)
        self.assertEqual(self.platform.actions, [{
            "action": "deploy_patch", "args": {"incident": "INC-1", "patch": "FIX-1"},
            "requested_by": "orchestrator", "evidence": {
                "sandbox_run_id": "SB-1", "risk_score": 8, "guardian_decision": "APPROVE"}}])
        states = [e["payload"]["status"] for e in self.platform.events if "status" in e["payload"]]
        self.assertEqual(states, ["investigating", "fix_proposed", "sandbox_validated", "reviewed", "gate"])
        self.assertEqual(self.platform.events[-1]["type"], "action_request")

    async def test_needs_testing_returns_to_engineer_with_feedback(self):
        review = dict(REVIEW, decision="NEEDS_TESTING")
        result = await self.build(patches=[PATCH, PATCH], reviews=[review, REVIEW]).run("INC-1")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(self.investigator.contexts), 1)
        self.assertEqual(self.engineer.contexts[1]["feedback"]["review"], review)
        self.assertEqual(self.engineer.contexts[1]["previous_patch"], PATCH)

    async def test_rejection_returns_to_investigator(self):
        review = dict(REVIEW, decision="REJECT")
        await self.build(diagnoses=[DIAGNOSIS, DIAGNOSIS], patches=[PATCH, PATCH],
                         reviews=[review, REVIEW]).run("INC-1")
        self.assertEqual(len(self.investigator.contexts), 2)
        self.assertEqual(self.investigator.contexts[1]["feedback"]["review"], review)

    async def test_failed_reproduction_or_validation_reinvestigates(self):
        for field in ("reproduced", "passed"):
            with self.subTest(field=field):
                result = await self.build(diagnoses=[DIAGNOSIS, DIAGNOSIS],
                                          patches=[dict(PATCH, **{field: False}), PATCH]).run("INC-1")
                self.assertEqual(result.attempts, 2)
                self.assertEqual(len(self.guardian.contexts), 1)
                self.assertEqual(len(self.investigator.contexts), 2)

    async def test_attempt_limit_escalates_without_submission(self):
        result = await self.build(reviews=[dict(REVIEW, decision="REJECT")], max_attempts=1).run("INC-1")
        self.assertEqual(result.status, "escalated")
        self.assertEqual(self.platform.actions, [])
        self.assertTrue(self.platform.events[-1]["payload"]["requires_human"])

    async def test_fabricated_approval_missing_or_mismatched_evidence_is_blocked(self):
        variants = [{}, {**EVIDENCE, "get_test_results": dict(EVIDENCE["get_test_results"], passed=False)},
                    {**EVIDENCE, "get_sandbox_results": dict(EVIDENCE["get_sandbox_results"], patch_ref="OLD")},
                    {**EVIDENCE, "get_diff": dict(EVIDENCE["get_diff"], diff="other diff")}]
        for evidence in variants:
            with self.subTest(evidence=evidence):
                result = await self.build(evidence=evidence).run("INC-1")
                self.assertEqual(result.status, "escalated")
                self.assertEqual(self.platform.actions, [])

    async def test_invalid_agent_output_escalates(self):
        result = await self.build(diagnoses=[dict(DIAGNOSIS, confidence=2)]).run("INC-1")
        self.assertEqual(result.status, "escalated")
        self.assertEqual(self.engineer.contexts, [])

    async def test_agent_step_failure_escalates(self):
        result = await self.build(diagnoses=[AgentError("step limit")]).run("INC-1")
        self.assertEqual(result.status, "escalated")
        self.assertIn("step limit", result.reason)

    async def test_uncertain_tool_error_never_retries(self):
        pipeline = self.build(patches=[ToolAPIError("uncertain write")])
        with self.assertRaises(ToolAPIError):
            await pipeline.run("INC-1")
        self.assertEqual(len(self.engineer.contexts), 1)
        self.assertEqual(self.platform.actions, [])

    async def test_event_failure_blocks_progress(self):
        pipeline = self.build()
        async def fail(*args):
            raise ToolAPIError("audit unavailable")
        self.platform.append_event = fail
        with self.assertRaises(ToolAPIError):
            await pipeline.run("INC-1")
        self.assertEqual(self.investigator.contexts, [])

    async def test_uncertain_submission_is_not_repeated(self):
        pipeline = self.build()
        async def fail(action):
            self.platform.actions.append(action)
            raise ToolAPIError("uncertain submission")
        self.platform.submit_action = fail
        with self.assertRaises(ToolAPIError):
            await pipeline.run("INC-1")
        self.assertEqual(len(self.platform.actions), 1)

    async def test_existing_gate_ticket_is_not_replayed(self):
        pipeline = self.build()
        self.platform.ticket["status"] = "awaiting_approval"
        with self.assertRaises(ValueError):
            await pipeline.run("INC-1")
        self.assertEqual(self.platform.actions, [])

    async def test_duplicate_run_and_deadline(self):
        pipeline = self.build(timeout_s=0.05)
        entered = asyncio.Event()
        async def wait(ticket_id):
            entered.set()
            await asyncio.sleep(1)
        self.platform.get_ticket = wait
        task = asyncio.create_task(pipeline.run("INC-1"))
        await entered.wait()
        with self.assertRaisesRegex(AgentError, "already running"):
            await pipeline.run("INC-1")
        with self.assertRaises(TimeoutError):
            await task
        self.assertEqual(pipeline._active, set())

    async def test_actual_agent_loops_with_simulated_platform_and_model(self):
        class Model:
            def __init__(self, tools, output):
                self.tools, self.output, self.called = tools, output, False
            async def chat(self, messages, *, tools):
                if self.called:
                    return {"role": "assistant", "content": json.dumps(self.output)}
                self.called = True
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": name, "arguments": arguments}} for name, arguments in self.tools]}

        class Tools:
            def __init__(self, role):
                self.role = role
            async def request(self, method, path, arguments):
                return deepcopy({
                    "/api/tools/diff": EVIDENCE["get_diff"],
                    "/api/tools/test-results": EVIDENCE["get_test_results"],
                    "/api/tools/sandbox-results": EVIDENCE["get_sandbox_results"],
                }.get(path, {"evidence_ref": "logs:1", "passed": True}))

        investigator = Investigator(api_client=Tools("investigator"), llm=Model(
            [("get_logs", {"service": "orders-api"})], DIAGNOSIS))
        engineer = Engineer(api_client=Tools("engineer"), llm=Model(
            [("run_loadtest", {"ticket_id": "INC-1"}),
             ("apply_patch", {"ticket_id": "INC-1", "diff": PATCH["diff"]}),
             ("run_tests", {"ticket_id": "INC-1"})], PATCH))
        guardian = Guardian(api_client=Tools("guardian"), llm=Model(
            [(name, {"ticket_id": "INC-1"}) for name in EVIDENCE], REVIEW))
        platform = SimulatedPlatform()
        result = await Orchestrator(platform=platform, investigator=investigator,
                                    engineer=engineer, guardian=guardian).run("INC-1")
        self.assertEqual(result.status, "submitted")
        self.assertEqual(len(platform.actions), 1)


if __name__ == "__main__":
    unittest.main()
