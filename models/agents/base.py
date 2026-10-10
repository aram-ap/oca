"""Construct a role with its own prompt, scoped API client, and fresh run state."""

import os
from pathlib import Path

from models.core.agent import Agent
from models.core.llm import OllamaClient
from models.tools.catalog import build_registry
from models.tools.client import ToolAPIClient


class RoleAgent(Agent):
    ROLE: str
    OUTPUT_SCHEMA: dict

    def __init__(self, *, llm=None, api_client=None, registry=None, **limits):
        if api_client is not None and api_client.role != self.ROLE:
            raise ValueError("API client role must match the agent")
        if registry is None:
            api_client = api_client or ToolAPIClient(
                role=self.ROLE, token=os.getenv(f"OCE_{self.ROLE.upper()}_TOKEN", ""),
                base_url=os.getenv("OCE_API_URL", "http://localhost:8001"),
                timeout_s=float(os.getenv("OCE_TOOL_TIMEOUT_S", "30")),
            )
            registry = build_registry(api_client)
        prompt = (Path(__file__).resolve().parents[1] / "prompts" / f"{self.ROLE}.txt").read_text(encoding="utf-8")
        super().__init__(role=self.ROLE, prompt=prompt, output_schema=self.OUTPUT_SCHEMA,
                         llm=llm or OllamaClient(), registry=registry, **limits)
