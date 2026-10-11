"""Minimal Prometheus HTTP API client (instant queries)."""

from __future__ import annotations

from typing import Any

import httpx


class PromClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def instant(self, expr: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/api/v1/query", params={"query": expr})
            response.raise_for_status()
            body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {body.get('error', 'unknown error')}")
        return list(body["data"].get("result", []))

    async def scalar(self, expr: str) -> float | None:
        result = await self.instant(expr)
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
