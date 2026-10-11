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

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status, self.body = status, body


def _http_json(base_url: str, token: str, method: str, path: str, arguments: dict | None, timeout_s: float):
    url = base_url.rstrip("/") + path
    data = None
    if method == "GET":
        if arguments:
            url += "?" + urlencode(arguments)
    else:
        data = json.dumps(arguments or {}).encode()
    request = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout_s) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ToolAPIError("Main Process response exceeds size limit")
            return json.loads(raw) if raw.strip() else None
    except HTTPError as exc:
        body = exc.read(2000).decode(errors="replace") if exc.fp else ""
        exc.close()
        detail = body.strip()[:300]
        raise ToolAPIError(f"Main Process request failed (HTTP {exc.code}){': ' + detail if detail else ''}; no retry",
                           exc.code, body) from exc
    except (URLError, OSError, ValueError) as exc:
        raise ToolAPIError("Main Process request failed; no retry") from exc


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
        return _http_json(self.base_url, self.token, method, path, arguments, self.timeout_s)


PLATFORM_PREFIXES = ("/api/tickets/", "/api/sandbox/", "/api/actions")


@dataclass(frozen=True)
class PlatformClient:
    """Orchestrator-scoped client for ticket state, sandbox lifecycle and the gate."""

    token: str = field(repr=False)
    base_url: str = "http://localhost:8001"
    timeout_s: float = 1800
    role: str = "orchestrator"

    def __post_init__(self):
        ModelConfig(ollama_url=self.base_url, timeout_s=self.timeout_s)
        if not self.token or any(c in self.token for c in "\r\n"):
            raise ValueError("The orchestrator token is required")

    async def request(self, method: str, path: str, arguments: dict | None = None):
        if method not in {"GET", "POST"} or not path.startswith(PLATFORM_PREFIXES):
            raise ValueError("Only fixed Main Process platform endpoints are supported")
        return await asyncio.to_thread(_http_json, self.base_url, self.token, method, path, arguments, self.timeout_s)
