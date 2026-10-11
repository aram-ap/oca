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

    def __init__(self, message: str, observations: list[Observation] | None = None):
        super().__init__(message)
        self.observations = list(observations or [])


class Agent:
    def __init__(self, *, role: str, prompt: str, output_schema: dict,
                 llm: Any, registry: ToolRegistry, max_steps: int = 12,
                 timeout_s: float = 300, tool_timeout_s: float = 30,
                 max_observation_chars: int = 20000, on_observation=None,
                 required_tools: tuple[str, ...] = (), mutating_tools: tuple[str, ...] = (),
                 nudge_after: int | None = None):
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
        # A role may be required to use one of ``required_tools`` before it can finish (the Engineer must patch).
        # ``mutating_tools`` change the world, so an identical call is allowed again after one of them succeeds.
        self.required_tools, self.mutating_tools = tuple(required_tools), tuple(mutating_tools)
        self.nudge_after = nudge_after if nudge_after is not None else max(1, max_steps // 2)

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
        seen: dict[str, tuple[int, Any]] = {}  # identical calls since the last mutation -> (step, result)
        nudged_at: set[int] = set()
        for step in range(1, self.max_steps + 1):
            remaining = self.max_steps - step
            if self.required_tools and not self._required_done(observations):
                if step == self.nudge_after + 1 or (remaining == 1 and step not in nudged_at):
                    nudged_at.add(step)
                    messages.append({"role": "user", "content": (
                        f"Budget check: {remaining + 1} step(s) left and you have not yet called "
                        f"{' / '.join(self.required_tools)}. Do that now; repeating read-only tools will not change "
                        "the result. Then verify and return the final JSON.")})
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
                    key = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
                    try:
                        if isinstance(arguments, dict) and "ticket_id" in arguments:
                            if arguments["ticket_id"] != context.get("ticket_id"):
                                raise ToolError("Tool ticket_id must match this incident")
                        if key in seen:
                            prior_step, prior = seen[key]
                            raise ToolError(f"Identical {name} call already ran at step {prior_step} and nothing has "
                                            f"changed since; its result was: {json.dumps(prior, default=str)[:1500]}. "
                                            "Do something different.")
                        result = await asyncio.wait_for(
                            self.registry.dispatch(self.role, name, arguments), self.tool_timeout_s)
                        ok = True
                        if name in self.mutating_tools and self._mutation_succeeded(result):
                            seen.clear()
                        else:
                            seen[key] = (step, result)
                    except ToolError as exc:
                        result, ok = {"error": str(exc)}, False
                    content = json.dumps({"untrusted_tool_result": result, "steps_remaining": remaining}, allow_nan=False)
                    if len(content) > self.max_observation_chars:
                        # Too big to feed the model: keep a preview and tell it to narrow the request.
                        result, ok = {"error": (f"Tool output ({len(content)} chars) exceeds the observation budget "
                                                f"({self.max_observation_chars}); request narrower evidence, e.g. fewer lines"),
                                      "preview": content[:1500]}, False
                        content = json.dumps({"untrusted_tool_result": result, "steps_remaining": remaining}, allow_nan=False)
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
            if (self.required_tools and not self._required_done(observations) and remaining > 0
                    and output.get("reproduced") is not False):
                messages.append({"role": "user", "content": (
                    f"Not finished: no successful {' / '.join(self.required_tools)} call yet, so this answer claims a fix "
                    "that was never applied. Apply the fix now, or return reproduced=false if the incident is absent.")})
                continue
            # A narrative answer without a successful tool observation is not evidence.
            if not any(observation.ok for observation in observations):
                messages.append({"role": "user", "content":
                                 "Gather evidence with your allowed tools before returning a final result."})
                continue
            return AgentResult(self.role, output, step, observations, messages)
        raise AgentError(f"{self.role} exhausted its {self.max_steps} steps", observations)

    def _required_done(self, observations: list[Observation]) -> bool:
        return any(o.ok and o.tool in self.required_tools and self._mutation_succeeded(o.result) for o in observations)

    @staticmethod
    def _mutation_succeeded(result: Any) -> bool:
        return not (isinstance(result, dict) and result.get("applied") is False)

    async def _notify(self, observation: Observation) -> None:
        if self.on_observation is None:
            return
        try:
            await self.on_observation(self.role, observation)
        except Exception:  # noqa: BLE001 - telemetry must never break the agent loop
            pass
