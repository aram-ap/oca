"""Behavioral tests for reasoning loops, budgets and role boundaries."""

import asyncio
import json
import unittest
from copy import deepcopy

from models.agents import Engineer, Guardian, Investigator
from models.core.agent import AgentError
from models.core.tool_registry import ToolError, ToolRegistry
from models.tools.catalog import build_registry
from models.tools.client import ToolAPIClient


DIAGNOSIS = {"diagnosis": "Timeout changed in deployment", "evidence_refs": ["logs:1"],
             "confidence": 0.9, "unresolved_questions": []}
PATCH = {"patch_ref": "fix:1", "diff": "diff:1", "evidence_refs": ["sandbox:1"],
         "reproduced": True, "passed": True, "test_commands": ["pytest"],
         "before": {"error_rate": 0.25}, "after": {"error_rate": 0}}
REVIEW = {"risk_score": 8, "policy_concerns": [], "decision": "APPROVE",
          "justification": "Sandbox evidence supports the patch", "evidence_refs": ["sandbox:1"]}


def final(output):
    return {"role": "assistant", "content": json.dumps(output)}


def call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]}


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.histories = []
        self.schemas = []

    async def chat(self, messages, *, tools):
        self.histories.append(deepcopy(messages))
        self.schemas.append(deepcopy(tools))
        return deepcopy(self.replies.pop(0))


class FakeAPI:
    def __init__(self, role):
        self.role, self.requests = role, []

    async def request(self, method, path, arguments):
        self.requests.append((method, path, arguments))
        return {"evidence_ref": "logs:1", "healthy": False}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_role_uses_tools_then_returns_structured_output(self):
        cases = [
            (Investigator, call("get_logs", service="orders-api"), DIAGNOSIS),
            (Engineer, call("run_loadtest", ticket_id="INC-1"), PATCH),
            (Guardian, call("get_diff", ticket_id="INC-1"), REVIEW),
        ]
        for cls, tool_call, output in cases:
            with self.subTest(role=cls.ROLE):
                api = FakeAPI(cls.ROLE)
                model = ScriptedModel(tool_call, final(output))
                result = await cls(llm=model, api_client=api).run({"ticket_id": "INC-1"})
                self.assertEqual(result.output, output)
                self.assertEqual(result.steps, 2)
                self.assertTrue(result.observations[0].ok)
                self.assertEqual(model.histories[1][-1]["role"], "tool")
                self.assertIn("untrusted_tool_result", model.histories[1][-1]["content"])

    async def test_forbidden_tools_never_reach_api(self):
        for cls, forbidden in [(Investigator, "apply_patch"), (Guardian, "apply_patch"),
                               (Engineer, "deploy_patch"), (Guardian, "submit_action")]:
            with self.subTest(role=cls.ROLE, tool=forbidden):
                api = FakeAPI(cls.ROLE)
                agent = cls(llm=ScriptedModel(call(forbidden)), api_client=api, max_steps=1)
                with self.assertRaises(AgentError):
                    await agent.run({"ticket_id": "INC-1"})
                self.assertEqual(api.requests, [])

    async def test_bad_arguments_and_cross_ticket_access_are_denied(self):
        for arguments in [{"ticket_id": "INC-2"}, {"ticket_id": "INC-1", "command": "rm"},
                          {"ticket_id": "INC-1", "requests": True}]:
            with self.subTest(arguments=arguments):
                api = FakeAPI("engineer")
                agent = Engineer(llm=ScriptedModel(call("run_loadtest", **arguments)),
                                 api_client=api, max_steps=1)
                with self.assertRaises(AgentError):
                    await agent.run({"ticket_id": "INC-1"})
                self.assertEqual(api.requests, [])

    async def test_invalid_output_is_corrected_within_budget(self):
        api = FakeAPI("guardian")
        invalid = dict(REVIEW, risk_score=101)
        model = ScriptedModel(call("get_diff", ticket_id="INC-1"), final(invalid), final(REVIEW))
        result = await Guardian(llm=model, api_client=api).run({"ticket_id": "INC-1"})
        self.assertEqual(result.steps, 3)
        self.assertIn("Invalid final output", model.histories[-1][-1]["content"])

    async def test_no_evidence_cannot_complete(self):
        agent = Investigator(llm=ScriptedModel(final(DIAGNOSIS)),
                             api_client=FakeAPI("investigator"), max_steps=1)
        with self.assertRaises(AgentError):
            await agent.run({"ticket_id": "INC-1"})

    async def test_run_deadline(self):
        class SlowModel:
            async def chat(self, *args, **kwargs):
                await asyncio.sleep(1)
        agent = Investigator(llm=SlowModel(), api_client=FakeAPI("investigator"), timeout_s=0.01)
        with self.assertRaisesRegex(AgentError, "deadline"):
            await agent.run({"ticket_id": "INC-1"})

    async def test_history_is_fresh_for_each_run(self):
        model = ScriptedModel(call("get_logs", service="orders-api"), final(DIAGNOSIS),
                              call("get_logs", service="orders-api"), final(DIAGNOSIS))
        agent = Investigator(llm=model, api_client=FakeAPI("investigator"))
        await agent.run({"ticket_id": "INC-1"})
        await agent.run({"ticket_id": "INC-2"})
        self.assertEqual(len(model.histories[2]), 2)
        self.assertNotIn("INC-1", json.dumps(model.histories[2]))

    async def test_observation_budget(self):
        agent = Investigator(llm=ScriptedModel(call("get_logs", service="orders-api")),
                             api_client=FakeAPI("investigator"), max_observation_chars=1)
        with self.assertRaisesRegex(AgentError, "observation budget"):
            await agent.run({"ticket_id": "INC-1"})

    async def test_engineer_iterates_after_failed_validation(self):
        class SandboxAPI(FakeAPI):
            async def request(self, method, path, arguments):
                self.requests.append((method, path, arguments))
                return {"passed": len(self.requests) > 3, "evidence_ref": "sandbox:1"}
        api = SandboxAPI("engineer")
        model = ScriptedModel(
            call("run_loadtest", ticket_id="INC-1"),
            call("apply_patch", ticket_id="INC-1", diff="first patch"),
            call("run_tests", ticket_id="INC-1"),
            call("apply_patch", ticket_id="INC-1", diff="revised patch"),
            call("run_loadtest", ticket_id="INC-1"),
            call("run_tests", ticket_id="INC-1"), final(PATCH),
        )
        result = await Engineer(llm=model, api_client=api).run({"ticket_id": "INC-1"})
        self.assertEqual(result.steps, 7)
        self.assertEqual(len(result.observations), 6)
        self.assertTrue(all(path.startswith("/api/tools/sandbox/") for _, path, _ in api.requests))

    async def test_uncertain_write_error_aborts_without_retry(self):
        class FailingAPI(FakeAPI):
            async def request(self, *args):
                self.requests.append(args)
                raise RuntimeError("uncertain outcome")
        api = FailingAPI("engineer")
        agent = Engineer(llm=ScriptedModel(call("apply_patch", ticket_id="INC-1", diff="patch")),
                         api_client=api)
        with self.assertRaisesRegex(RuntimeError, "uncertain outcome"):
            await agent.run({"ticket_id": "INC-1"})
        self.assertEqual(len(api.requests), 1)

    async def test_role_catalogs_and_dispatch_both_enforce_permissions(self):
        expected = {
            "investigator": {"get_logs", "get_containers", "get_metrics", "get_code", "get_git"},
            "engineer": {"get_code", "get_git", "apply_patch", "run_tests", "run_loadtest"},
            "guardian": {"get_diff", "get_test_results", "get_sandbox_results"},
        }
        for role, names in expected.items():
            registry = build_registry(FakeAPI(role))
            self.assertEqual({t["function"]["name"] for t in registry.schemas(role)}, names)
            with self.assertRaises(ToolError):
                await registry.dispatch(role, "deploy_patch", {})
        # A registry containing an Engineer tool still denies the Investigator.
        registry = build_registry(FakeAPI("engineer"))
        with self.assertRaises(ToolError):
            await registry.dispatch("investigator", "apply_patch", {"ticket_id": "INC-1", "diff": "x"})

    async def test_wrong_role_client_and_missing_credentials_fail(self):
        with self.assertRaises(ValueError):
            Investigator(api_client=FakeAPI("engineer"))
        with self.assertRaises(ValueError):
            ToolAPIClient(role="guardian", token="")
        self.assertNotIn("secret", repr(ToolAPIClient(role="guardian", token="secret")))


if __name__ == "__main__":
    unittest.main()
