"""Orchestrator state machine with a recorded fake platform and canned agents (no oca, no network)."""

import asyncio
import unittest

from models.core.agent import AgentError
from models.core.state import AgentResult, Observation
from models.orchestrator import Orchestrator, config_change
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


class Canned:
    """Agent stand-in: returns scripted outputs (or raises) in order."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.contexts = []

    async def run(self, context):
        self.contexts.append(context)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return AgentResult("x", item, 2, [Observation("get_logs", {}, {"evidence_ref": "logs:1"}, True)])


def orchestrator(platform, inv, eng, guard, attempts=2):
    return Orchestrator(platform, {"investigator": inv, "engineer": eng, "guardian": guard}, max_attempts=attempts)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_submits_set_config(self):
        platform = FakePlatform(validations=[True])
        status = await orchestrator(platform, Canned(DIAG), Canned(PATCH), Canned(APPROVE)).run_ticket("INC-0001")
        self.assertEqual(status, "awaiting_approval")
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
        status = await orchestrator(platform, Canned(DIAG), eng, Canned(APPROVE)).run_ticket("INC-0001")
        self.assertEqual(status, "awaiting_approval")
        self.assertEqual(len(eng.contexts), 2)
        self.assertIn("Sandbox validation failed", eng.contexts[1]["feedback"][0])
        action = [b for m, p, b in platform.calls if p == "/api/actions"][0]
        self.assertEqual(action["action"], "deploy_patch")
        self.assertEqual(action["args"]["branch"], "fix/INC-0001")

    async def test_needs_testing_loops_engineer_then_approves(self):
        platform = FakePlatform(validations=[True, True])
        needs = dict(APPROVE, decision="NEEDS_TESTING", policy_concerns=["no checkout test"])
        guard = Canned(needs, APPROVE)
        eng = Canned(PATCH, PATCH)
        status = await orchestrator(platform, Canned(DIAG), eng, guard).run_ticket("INC-0001")
        self.assertEqual(status, "awaiting_approval")
        self.assertEqual(len(guard.contexts), 2)
        self.assertIn("Guardian requested more testing", eng.contexts[1]["feedback"][0])

    async def test_reject_and_agent_errors_exhaust_attempts(self):
        platform = FakePlatform(validations=[True, True])
        reject = dict(APPROVE, decision="REJECT", justification="wrong fix")
        inv = Canned(DIAG, AgentError("investigator exhausted its 12 steps"))
        status = await orchestrator(platform, inv, Canned(PATCH, PATCH), Canned(reject, reject)).run_ticket("INC-0001")
        self.assertEqual(status, "failed")
        self.assertEqual(platform.statuses()[-1], "failed")
        final = platform.events("error")[-1]["payload"]
        self.assertIn("REJECT", final["attempts"][0])
        self.assertIn("investigator", final["attempts"][1])
        self.assertTrue(any(p.endswith("/teardown") for m, p, b in platform.calls))
        self.assertIn("Guardian REJECT", inv.contexts[1]["feedback"][0])

    async def test_not_reproduced_and_missing_patch(self):
        platform = FakePlatform(reproduced=False)
        status = await orchestrator(platform, Canned(DIAG), Canned(PATCH), Canned(APPROVE), attempts=1).run_ticket("INC-0001")
        self.assertEqual(status, "failed")
        self.assertNotIn("fix_proposed", platform.statuses())
        platform = FakePlatform(validations=[])  # validate always 409: engineer never applied a patch
        eng = Canned(dict(PATCH, patch_ref="none", diff="none"), dict(PATCH, patch_ref="none", diff="none"))
        status = await orchestrator(platform, Canned(DIAG), eng, Canned(APPROVE), attempts=1).run_ticket("INC-0001")
        self.assertEqual(status, "failed")
        self.assertIn("apply a patch", eng.contexts[1]["feedback"][-1])

    def test_config_change_detection(self):
        self.assertEqual(config_change(REVERT), ("DB_TIMEOUT_MS", 3000))
        self.assertIsNone(config_change(CODE))
        self.assertIsNone(config_change(REVERT + "+DB_POOL_MAX=20\n"))
        self.assertIsNone(config_change(REVERT.replace("DB_TIMEOUT_MS", "SECRET")))


if __name__ == "__main__":
    unittest.main()
