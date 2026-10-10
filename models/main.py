"""Development smoke checks: python -m models.main --check [--tools]."""

import argparse
import asyncio
import sys

from models.config import ModelConfig
from models.core.llm import OllamaClient, OllamaError


async def check_model(config: ModelConfig, check_tools: bool = False) -> None:
    client = OllamaClient(config)
    reply = await client.chat([{"role": "user", "content": "Reply with exactly READY."}])
    if reply.get("content", "").strip() != "READY" or reply.get("tool_calls"):
        raise OllamaError("Inference check failed: expected READY without tool calls")
    print(f"Inference passed: {config.model}")
    if not check_tools:
        return
    tool = {"type": "function", "function": {
        "name": "get_service_health",
        "description": "Return the current health of a service.",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}},
                       "required": ["service"], "additionalProperties": False},
    }}
    messages = [{"role": "user", "content": (
        "Use get_service_health for orders-api. After receiving the result, "
        "reply with exactly HEALTHY if it is healthy."
    )}]
    reply = await client.chat(messages, tools=[tool])
    calls = reply.get("tool_calls", [])
    if (len(calls) != 1 or calls[0]["function"]["name"] != "get_service_health"
            or calls[0]["function"]["arguments"] != {"service": "orders-api"}):
        raise OllamaError("Tool check failed: expected get_service_health(service='orders-api')")
    # Only this fixed synthetic result is supplied; no infrastructure is accessed.
    messages.extend([reply, {"role": "tool", "tool_name": "get_service_health",
                             "content": '{"service":"orders-api","healthy":true}'}])
    final = await client.chat(messages, tools=[tool])
    if final.get("tool_calls") or final.get("content", "").strip() != "HEALTHY":
        raise OllamaError("Tool check failed: model did not consume the synthetic health result")
    print("Tool calling passed (synthetic health result)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check local model inference")
    parser.add_argument("--tools", action="store_true", help="Also check a synthetic tool round trip")
    args = parser.parse_args()
    if not args.check:
        parser.error("Use --check; ticket orchestration is not implemented yet")
    try:
        asyncio.run(check_model(ModelConfig.from_env(), args.tools))
    except (OllamaError, ValueError) as exc:
        print(f"Model check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
