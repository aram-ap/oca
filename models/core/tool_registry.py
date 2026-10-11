"""Typed tools, role allowlists, and validation before dispatch."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import math

ROLES = frozenset({"investigator", "engineer", "guardian"})


class ToolError(RuntimeError):
    """An invalid or unavailable tool request."""


def validate(value: Any, schema: dict, path: str = "value") -> None:
    """Validate the JSON schema subset used by OCA's registered tools/outputs.

    Supports object, array, string, boolean, integer, number, enum, required,
    additionalProperties, minimum/maximum and minLength. No coercion.
    """
    kind = schema["type"]
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": type(value) is bool,
        "integer": type(value) is int, "number": type(value) in {int, float},
    }
    if kind not in matches or not matches[kind]:
        raise ToolError(f"{path} must be {kind}")
    if kind in {"integer", "number"}:
        if not math.isfinite(value):
            raise ToolError(f"{path} must be finite")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ToolError(f"{path} is outside the allowed range")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{path} is not an allowed value")
    if kind == "string" and len(value) < schema.get("minLength", 0):
        raise ToolError(f"{path} must not be empty")
    if kind == "array":
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise ToolError(f"{path} is missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ToolError(f"{path} contains unknown fields")
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{path}.{key}")


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    roles: frozenset[str]
    handler: Callable[..., Awaitable[Any]]


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {}
        for tool in tools:
            if tool.name in self._tools or not tool.roles or not tool.roles <= ROLES:
                raise ValueError("Tools need unique names and valid role allowlists")
            self._tools[tool.name] = tool

    def schemas(self, role: str) -> list[dict]:
        if role not in ROLES:
            raise ValueError("Unknown agent role")
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description,
            "parameters": deepcopy(tool.parameters),
        }} for tool in self._tools.values() if role in tool.roles]

    async def dispatch(self, role: str, name: str, arguments: dict) -> Any:
        tool = self._tools.get(name) or (self._tools.get(name.strip().lower()) if isinstance(name, str) else None)
        if tool is None or role not in tool.roles:
            raise ToolError("Tool is not allowed for this agent")
        validate(arguments, tool.parameters, "arguments")
        return await tool.handler(**arguments)
