"""Role-scoped HTTP client for Main Process tools; no shell or direct file writes."""

import asyncio
import json
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from models.config import ModelConfig
from models.core.llm import _NoRedirect
from models.core.tool_registry import ROLES


class ToolAPIError(RuntimeError):
    """Transport failures abort the run: a write may have already taken effect."""


@dataclass(frozen=True)
class ToolAPIClient:
    role: str
    token: str = field(repr=False)
    base_url: str = "http://localhost:8001"
    timeout_s: float = 30

    def __post_init__(self):
        ModelConfig(ollama_url=self.base_url, timeout_s=self.timeout_s)
        if self.role not in ROLES or not self.token or any(c in self.token for c in "\r\n"):
            raise ValueError("A valid role and its own bearer token are required")

    async def request(self, method: str, path: str, arguments: dict):
        if method not in {"GET", "POST"} or not path.startswith("/api/tools/"):
            raise ValueError("Only fixed Main Process tool endpoints are supported")
        return await asyncio.to_thread(self._request, method, path, arguments)

    def _request(self, method, path, arguments):
        url = self.base_url.rstrip("/") + path
        data = None
        if method == "GET":
            url += "?" + urlencode(arguments)
        else:
            data = json.dumps(arguments).encode()
        request = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                    request, timeout=self.timeout_s) as response:
                raw = response.read(100001)
                if len(raw) > 100000:
                    raise ToolAPIError("Main Process response exceeds size limit")
                return json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise ToolAPIError(f"Main Process tool request failed (HTTP {exc.code}); no retry") from exc
        except (URLError, OSError, ValueError) as exc:
            raise ToolAPIError("Main Process tool request failed; no retry") from exc
