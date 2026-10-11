"""Orchestrator state machine with a recorded fake platform and canned agents (no oca, no network)."""

import asyncio
import unittest

from models.core.agent import AgentError
from models.core.state import AgentResult, Observation
from models.orchestrator import Orchestrator, PipelineResult, config_change
from models.tools.client import ToolAPIError

REVERT = "--- a/services/orders-api/config/app.env\n+++ b/services/orders-api/config/app.env\n@@ -3 +3 @@\n-DB_TIMEOUT_MS=100\n+DB_TIMEOUT_MS=3000\n"
CODE = "--- /dev/null\n+++ b/services/orders-api/migrations/002_idx.sql\n@@ -0,0 +1 @@\n+CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON orders (customer_email);\n"
DIAG = {"diagnosis": "DB_TIMEOUT_MS lowered to 100 in deploy 2", "evidence_refs": ["git:diff:2"], "confidence": 0.9, "unresolved_questions": []}
PATCH = {"patch_ref": "fix/INC-0001", "diff": REVERT, "evidence_refs": ["sandbox:SBX-0001:tests"], "reproduced": True,
         "passed": True, "test_commands": ["pytest"], "before": {"error_rate_pct": 25}, "after": {"error_rate_pct": 0}}
APPROVE = {"risk_score": 8, "policy_concerns": [], "decision": "APPROVE", "justification": "config revert", "evidence_refs": ["sandbox:SBX-0001:diff"]}
TICKET = {"id": "INC-0001", "service": "orders-api", "title": "errors", "severity": "critical", "source": "health_check",
          "metrics_at_detect": {"error_rate_pct": 25.0}, "metrics_before": {},
          "events": [{"type": "detected", "payload": {"check": "status_check", "detail": "INCIDENT", "status": {"git_sha": "f6e5"}}}]}


class FakePlatform:
    def __init__(self, *, reproduced=True, validations=None):
        self.calls = []
        self.reproduced = reproduced
        self.validations = list(validations or [])
        self.run = {"id": "SBX-0001", "base_sha": "f6e5", "reproduced": reproduced, "patch_ref": None, "before": {"error_rate_pct": 25.0}}

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/tickets/INC-0001":
            return TICKET
        if path.endswith("/prepare"):
            return dict(self.run)
        if path.endswith("/validate"):
            if not self.validations:
                raise ToolAPIError("conflict", 409, "no patch has been applied in the sandbox")
            passed = self.validations.pop(0)
            return {**self.run, "patch_ref": "fix/INC-0001", "passed": passed, "after": {"error_rate_pct": 0 if passed else 20},
                    "tests": {"passed": passed, "total": 7, "failed": 0 if passed else 1, "output_tail": ""}}
        if path == "/api/actions":
            return {"status": "awaiting_approval", "reason": "policy passed"}
        return None

    def statuses(self):
        return [b["status"] for m, p, b in self.calls if p.endswith("/status")]

    def events(self, type_=None):
        return [b for m, p, b in self.calls if p.endswith("/events") and (type_ is None or b["type"] == type_)]


def guardian_evidence(run_id="SBX-0001", patch_ref="fix/INC-0001", tests_passed=True, passed=True):
    """Observations a diligent Guardian produces: server evidence for this exact run and patch."""
    return [
        Observation("get_diff", {"ticket_id": "INC-0001"}, {"sandbox_run_id": run_id, "patch_ref": patch_ref, "diff": REVERT}, True),
        Observation("get_test_results", {"ticket_id": "INC-0001"}, {"sandbox_run_id": run_id, "patch_ref": patch_ref,
                                                                    "tests": {"passed": tests_passed}}, True),
        Observation("get_sandbox_results", {"ticket_id": "INC-0001"}, {"id": run_id, "patch_ref": patch_ref,
                                                                       "reproduced": True, "passed": passed}, True),
    ]


class Canned:
    """Agent stand-in: returns scripted outputs (or raises) in order."""

    def __init__(self, *outputs, observations=None):
        self.outputs = list(outputs)
        self.contexts = []
        self.observations = observations

    async def run(self, context):
        self.contexts.append(context)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        observations = self.observations if self.observations is not None else [
            Observation("get_logs", {}, {"evidence_ref": "logs:1"}, True)]
        return AgentResult("x", item, 2, list(observations))


def guard(*outputs, observations=None):
    return Canned(*outputs, observations=guardian_evidence() if observations is None else observations)


def orchestrator(platform, inv, eng, guardian, attempts=2, **limits):
    return Orchestrator(platform, {"investigator": inv, "engineer": eng, "guardian": guardian}, max_attempts=attempts, **limits)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_submits_set_config(self):
        platform = FakePlatform(validations=[True])
        result = await orchestrator(platform, Canned(DIAG), Canned(PATCH), guard(APPROVE)).run_ticket("INC-0001")
        self.assertIsInstance(result, PipelineResult)
        self.assertEqual((result.status, result.attempts, result.submitted), ("awaiting_approval", 1, True))
        self.assertEqual(result.gate_response["reason"], "policy passed")
        self.assertEqual(platform.statuses(), ["investigating", "fix_proposed", "sandbox_validated", "reviewed"])
        self.assertEqual([e["type"] for e in platform.events()], ["diagnosis", "patch", "review"])
        self.assertEqual(platform.events("diagnosis")[0]["actor"], "investigator")
        action = [b for m, p, b in platform.calls if p == "/api/actions"][0]
        self.assertEqual(action["action"], "set_config")
        self.assertEqual(action["args"], {"service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000})
        self.assertEqual(action["evidence"]["sandbox_run_id"], "SBX-0001")
        self.assertEqual(action["evidence"]["guardian_decision"], "APPROVE")

    async def test_code_patch_submits_deploy_patch_and_feeds_feedback(self):
        platform = FakePlatform(validations=[False, True])
        eng = Canned(dict(PATCH, diff=CODE), dict(PATCH, diff=CODE))
        result = await orchestrator(platform, Canned(DIAG), eng, guard(APPROVE)).run_ticket("INC-0001")
        self.assertEqual(result.status, "awaiting_approval")
        self.assertEqual(len(eng.contexts), 2)
        self.assertIn("Sandbox validation failed", eng.contexts[1]["feedback"][0])
        action = [b for m, p, b in platform.calls if p == "/api/actions"][0]
        self.assertEqual(action["action"], "deploy_patch")
        self.assertEqual(action["args"]["branch"], "fix/INC-0001")

    async def test_needs_testing_loops_engineer_then_approves(self):
        platform = FakePlatform(validations=[True, True])
        needs = dict(APPROVE, decision="NEEDS_TESTING", policy_concerns=["no checkout test"])
        guardian = guard(needs, APPROVE)
        eng = Canned(PATCH, PATCH)
        result = await orchestrator(platform, Canned(DIAG), eng, guardian).run_ticket("INC-0001")
        self.assertEqual(result.status, "awaiting_approval")
        self.assertEqual(len(guardian.contexts), 2)
        self.assertIn("Guardian requested more testing", eng.contexts[1]["feedback"][0])

    async def test_reject_and_agent_errors_exhaust_attempts(self):
        platform = FakePlatform(validations=[True, True])
        reject = dict(APPROVE, decision="REJECT", justification="wrong fix")
        inv = Canned(DIAG, AgentError("investigator exhausted its 12 steps"))
        result = await orchestrator(platform, inv, Canned(PATCH, PATCH), guard(reject, reject)).run_ticket("INC-0001")
        self.assertEqual((result.status, result.submitted), ("failed", False))
        self.assertEqual(platform.statuses()[-1], "failed")
        final = platform.events("error")[-1]["payload"]
        self.assertIn("REJECT", final["attempts"][0])
        self.assertIn("investigator", final["attempts"][1])
        self.assertTrue(any(p.endswith("/teardown") for m, p, b in platform.calls))
        self.assertIn("Guardian REJECT", inv.contexts[1]["feedback"][0])

    async def test_not_reproduced_and_missing_patch(self):
        platform = FakePlatform(reproduced=False)
        result = await orchestrator(platform, Canned(DIAG), Canned(PATCH), guard(APPROVE), attempts=1).run_ticket("INC-0001")
        self.assertEqual(result.status, "failed")
        self.assertNotIn("fix_proposed", platform.statuses())
        platform = FakePlatform(validations=[])  # validate always 409: engineer never applied a patch
        eng = Canned(dict(PATCH, patch_ref="none", diff="none"), dict(PATCH, patch_ref="none", diff="none"))
        result = await orchestrator(platform, Canned(DIAG), eng, guard(APPROVE), attempts=1).run_ticket("INC-0001")
        self.assertEqual(result.status, "failed")
        self.assertIn("apply a patch", eng.contexts[1]["feedback"][-1])

    async def test_approval_without_server_evidence_is_audited(self):
        # A Guardian that approves without fetching evidence gets one retry with feedback, then the attempt is spent.
        platform = FakePlatform(validations=[True])
        lazy = Canned(APPROVE, APPROVE)  # default observations: only get_logs
        result = await orchestrator(platform, Canned(DIAG), Canned(PATCH), lazy, attempts=1).run_ticket("INC-0001")
        self.assertEqual(result.status, "failed")
        self.assertEqual(platform.calls.count(("POST", "/api/actions", None)), 0)
        self.assertTrue(all(p != "/api/actions" for m, p, b in platform.calls))
        self.assertIn("Guardian approved without consulting", lazy.contexts[1]["feedback"][-1])
        # Evidence for another run, failed tests, or a different patch never reaches the gate.
        for observations in (guardian_evidence(run_id="SBX-0009"), guardian_evidence(tests_passed=False),
                             guardian_evidence(patch_ref="fix/OTHER"), guardian_evidence(passed=False)):
            with self.subTest(observations=[o.result for o in observations]):
                platform = FakePlatform(validations=[True])
                sloppy = Canned(APPROVE, APPROVE, observations=observations)
                result = await orchestrator(platform, Canned(DIAG), Canned(PATCH), sloppy, attempts=1).run_ticket("INC-0001")
                self.assertEqual(result.status, "failed")
                self.assertTrue(all(p != "/api/actions" for m, p, b in platform.calls))
        # The second, diligent review passes the audit.
        platform = FakePlatform(validations=[True])
        learns = Canned(APPROVE, APPROVE)
        learns.observations = None
        outputs = iter([[Observation("get_logs", {}, {}, True)], guardian_evidence()])
        original = learns.run

        async def run(context):
            learns.observations = next(outputs)
            return await original(context)
        learns.run = run
        result = await orchestrator(platform, Canned(DIAG), Canned(PATCH), learns, attempts=1).run_ticket("INC-0001")
        self.assertEqual(result.status, "awaiting_approval")

    async def test_invalid_agent_output_spends_the_attempt(self):
        platform = FakePlatform(validations=[True])
        bad = Canned(dict(DIAG, confidence=2))
        result = await orchestrator(platform, bad, Canned(PATCH), guard(APPROVE), attempts=1).run_ticket("INC-0001")
        self.assertEqual(result.status, "failed")
        self.assertIn("confidence", platform.events("error")[0]["payload"]["error"])

    async def test_no_replay_duplicate_guard_and_deadline(self):
        platform = FakePlatform(validations=[True])
        pipeline = orchestrator(platform, Canned(DIAG), Canned(PATCH), guard(APPROVE))
        TICKET["status"] = "awaiting_approval"
        try:
            with self.assertRaisesRegex(ValueError, "must not run again"):
                await pipeline.run_ticket("INC-0001")
        finally:
            TICKET["status"] = "detected"
        self.assertEqual(platform.statuses(), [])

        slow = FakePlatform(validations=[True])
        entered = asyncio.Event()

        async def stall(method, path, body=None):
            entered.set()
            await asyncio.sleep(1)
        slow.request = stall
        pipeline = orchestrator(slow, Canned(DIAG), Canned(PATCH), guard(APPROVE), timeout_s=0.05)
        task = asyncio.create_task(pipeline.run_ticket("INC-0001"))
        await entered.wait()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            await pipeline.run_ticket("INC-0001")
        result = await task
        self.assertEqual(result.status, "failed")
        self.assertIn("deadline", result.reason)
        self.assertEqual(pipeline._active, set())

    async def test_uncertain_submission_is_not_repeated(self):
        platform = FakePlatform(validations=[True])
        attempts = []

        async def flaky(method, path, body=None):
            if path == "/api/actions":
                attempts.append(body)
                raise ToolAPIError("uncertain submission", 500, "")
            return await FakePlatform.request(platform, method, path, body)
        platform.request = flaky
        from models.orchestrator import PlatformError
        with self.assertRaises(PlatformError):
            await orchestrator(platform, Canned(DIAG), Canned(PATCH), guard(APPROVE)).run_ticket("INC-0001")
        self.assertEqual(len(attempts), 1)

    def test_config_change_detection(self):
        self.assertEqual(config_change(REVERT), ("DB_TIMEOUT_MS", 3000))
        self.assertIsNone(config_change(CODE))
        self.assertIsNone(config_change(REVERT + "+DB_POOL_MAX=20\n"))
        self.assertIsNone(config_change(REVERT.replace("DB_TIMEOUT_MS", "SECRET")))


if __name__ == "__main__":
    unittest.main()
