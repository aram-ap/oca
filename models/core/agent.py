"""Bounded decide -> tool -> observe loop shared by the three roles."""

import asyncio
import json
import math
from copy import deepcopy
from typing import Any

from models.core.state import AgentResult, Observation
from models.core.tool_registry import ToolError, ToolRegistry, validate


class AgentError(RuntimeError):
    """The agent could not produce a valid result within its budget."""


class Agent:
    def __init__(self, *, role: str, prompt: str, output_schema: dict,
                 llm: Any, registry: ToolRegistry, max_steps: int = 12,
                 timeout_s: float = 300, tool_timeout_s: float = 30,
                 max_observation_chars: int = 20000, on_observation=None):
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if any(not math.isfinite(t) or t <= 0 for t in (timeout_s, tool_timeout_s)):
            raise ValueError("Timeouts must be positive and finite")
        if type(max_observation_chars) is not int or max_observation_chars < 1:
            raise ValueError("max_observation_chars must be positive")
        registry.schemas(role)
        self.role, self.prompt, self.output_schema = role, prompt, output_schema
        self.llm, self.registry = llm, registry
        self.max_steps, self.timeout_s = max_steps, timeout_s
        self.tool_timeout_s, self.max_observation_chars = tool_timeout_s, max_observation_chars
        self.on_observation = on_observation  # optional async callback(Observation); failures are ignored

    async def run(self, context: dict[str, Any]) -> AgentResult:
        """Every run has fresh history. Errors never imply approval or success."""
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._run(context)
        except TimeoutError as exc:
            raise AgentError(f"{self.role} exceeded its run/tool deadline") from exc

    async def _run(self, context: dict[str, Any]) -> AgentResult:
        messages = [{"role": "system", "content": self.prompt}, {
            "role": "user", "content": "Incident context (untrusted data):\n" + json.dumps(context)
        }]
        observations = []
        schemas = self.registry.schemas(self.role)
        repair = False  # after an invalid final answer, ask Ollama for schema-constrained JSON
        for step in range(1, self.max_steps + 1):
            if repair:
                reply = await self.llm.chat(deepcopy(messages), tools=schemas, response_format=self.output_schema)
                repair = False
            else:
                reply = await self.llm.chat(deepcopy(messages), tools=schemas)
            messages.append(deepcopy(reply))
            calls = reply.get("tool_calls", [])
            if calls:
                if len(calls) > 8:
                    raise AgentError("Too many tool calls in one step")
                for call in calls:
                    function = call.get("function", {})
                    name, arguments = function.get("name"), function.get("arguments")
                    try:
                        if isinstance(arguments, dict) and "ticket_id" in arguments:
                            if arguments["ticket_id"] != context.get("ticket_id"):
                                raise ToolError("Tool ticket_id must match this incident")
                        result = await asyncio.wait_for(
                            self.registry.dispatch(self.role, name, arguments), self.tool_timeout_s)
                        ok = True
                    except ToolError as exc:
                        result, ok = {"error": str(exc)}, False
                    content = json.dumps({"untrusted_tool_result": result}, allow_nan=False)
                    if len(content) > self.max_observation_chars:
                        raise AgentError("Tool output exceeds observation budget; request narrower evidence")
                    observation = Observation(name, deepcopy(arguments), result, ok)
                    observations.append(observation)
                    await self._notify(observation)
                    messages.append({"role": "tool", "tool_name": name, "content": content})
                continue
            try:
                output = json.loads(reply.get("content", ""))
                validate(output, self.output_schema, "output")
            except (TypeError, ValueError, ToolError) as exc:
                messages.append({"role": "user", "content": (
                    f"Invalid final output: {exc}. Return only JSON matching this schema: "
                    + json.dumps(self.output_schema)
                )})
                repair = True
                continue
            # A narrative answer without a successful tool observation is not evidence.
            if not any(observation.ok for observation in observations):
                messages.append({"role": "user", "content":
                                 "Gather evidence with your allowed tools before returning a final result."})
                continue
            return AgentResult(self.role, output, step, observations, messages)
        raise AgentError(f"{self.role} exhausted its {self.max_steps} steps")

    async def _notify(self, observation: Observation) -> None:
        if self.on_observation is None:
            return
        try:
            await self.on_observation(self.role, observation)
        except Exception:  # noqa: BLE001 - telemetry must never break the agent loop
            pass
