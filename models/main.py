"""Development entry points.

    python3 -m models.main --check [--tools]      smoke-test the local model (and a synthetic tool round trip)
    python3 -m models.main --ticket INC-0042      run the agent pipeline against a running Main Process
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict

from models.config import ModelConfig, load_dotenv
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


async def run_ticket(ticket_id: str, api_url: str, max_attempts: int):
    from models.agents import Engineer, Guardian, Investigator
    from models.orchestrator import Orchestrator
    from models.tools.client import PlatformClient, ToolAPIClient

    token = os.getenv("OCA_ORCHESTRATOR_TOKEN", "")
    if not token:
        raise ValueError("OCA_ORCHESTRATOR_TOKEN is required")
    llm = OllamaClient(ModelConfig.from_env())
    orchestrator = Orchestrator(PlatformClient(token=token, base_url=api_url), max_attempts=max_attempts)

    def client(role: str, timeout: float) -> ToolAPIClient:
        return ToolAPIClient(role=role, token=os.getenv(f"OCA_{role.upper()}_TOKEN", ""), base_url=api_url, timeout_s=timeout)

    orchestrator.agents = {
        "investigator": Investigator(llm=llm, api_client=client("investigator", 60), on_observation=orchestrator.observer,
                                     timeout_s=900, max_steps=16, max_observation_chars=40_000),
        "engineer": Engineer(llm=llm, api_client=client("engineer", 1500), on_observation=orchestrator.observer,
                             timeout_s=3600, tool_timeout_s=1500, max_steps=16, max_observation_chars=40_000),
        "guardian": Guardian(llm=llm, api_client=client("guardian", 60), on_observation=orchestrator.observer,
                             timeout_s=900, max_steps=16, max_observation_chars=40_000),
    }
    return await orchestrator.run_ticket(ticket_id)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Check local model inference")
    parser.add_argument("--tools", action="store_true", help="Also check a synthetic tool round trip")
    parser.add_argument("--ticket", metavar="INC-0042", help="Run the agent pipeline for this ticket")
    parser.add_argument("--api-url", default=os.getenv("OCA_API_URL", "http://localhost:8001"))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("OCA_MAX_ATTEMPTS", "2")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.ticket:
            result = asyncio.run(run_ticket(args.ticket, args.api_url, args.max_attempts))
            print(json.dumps(asdict(result), indent=2))
            return 0 if result.submitted else 2
        if not args.check:
            parser.error("Use --check or --ticket")
        asyncio.run(check_model(ModelConfig.from_env(), args.tools))
    except (OllamaError, ValueError, RuntimeError) as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
