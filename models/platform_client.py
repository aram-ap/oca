"""Orchestrator-only Main Process API access; no deployment execution."""

import asyncio
import os
from urllib.parse import quote

from models.config import ModelConfig
from models.tools.client import ToolAPIClient


class PlatformClient(ToolAPIClient):
    def __post_init__(self):
        ModelConfig(ollama_url=self.base_url, timeout_s=self.timeout_s)
        if self.role != "orchestrator" or not self.token or any(c in self.token for c in "\r\n"):
            raise ValueError("A separate orchestrator bearer token is required")

    @classmethod
    def from_env(cls):
        return cls(role="orchestrator", token=os.getenv("OCE_ORCHESTRATOR_TOKEN", ""),
                   base_url=os.getenv("OCE_API_URL", "http://localhost:8001"),
                   timeout_s=float(os.getenv("OCE_TOOL_TIMEOUT_S", "30")))

    async def request(self, method, path, arguments):
        # Reject generic inherited tool access; callers use the methods below.
        raise ValueError("Use get_ticket, append_event or submit_action")

    async def get_ticket(self, ticket_id):
        return await asyncio.to_thread(self._request, "GET",
                                       f"/api/tickets/{quote(ticket_id, safe='')}", {})

    async def append_event(self, ticket_id, event):
        return await asyncio.to_thread(self._request, "POST",
                                       f"/api/tickets/{quote(ticket_id, safe='')}/events", event)

    async def submit_action(self, action):
        return await asyncio.to_thread(self._request, "POST", "/api/actions", action)
