"""Async, non-streaming Ollama transport. This client never executes tools."""

import asyncio
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from models.config import ModelConfig


class OllamaError(RuntimeError):
    """The local service could not complete a valid chat response."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig.from_env()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the assistant message, preserving tool calls and thinking.

        Blocking HTTP runs in a worker thread so FastAPI's event loop stays free.
        Socket operations use the configured timeout; requests are not retried.
        """
        if not messages:
            raise ValueError("At least one chat message is required")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": {"num_predict": self.config.max_output_tokens},
        }
        if tools is not None:
            payload["tools"] = tools
        if response_format is not None:
            payload["format"] = response_format
        return await asyncio.to_thread(self._chat, payload)

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.config.ollama_url.rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        # Ignore system proxy settings and reject redirects: incident data stays local.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.config.timeout_s) as response:
                result = json.load(response)
        except HTTPError as exc:
            exc.close()
            raise OllamaError(
                f"Ollama returned HTTP {exc.code}; check the service and model {self.config.model!r}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OllamaError(
                "Cannot reach Ollama or request timed out; check the service and OCA_LLM_TIMEOUT_S"
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("error"):
            raise OllamaError("Ollama returned an error or invalid response object")
        message = result.get("message")
        if result.get("done") is not True or not isinstance(message, dict):
            raise OllamaError("Ollama returned an incomplete chat response")
        if message.get("role") != "assistant" or not isinstance(message.get("content", ""), str):
            raise OllamaError("Ollama returned an invalid assistant message")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise OllamaError("Ollama returned invalid tool calls")
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("arguments"), dict)):
                raise OllamaError("Ollama returned an invalid tool call")
        return message
