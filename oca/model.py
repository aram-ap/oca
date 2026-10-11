"""Which Ollama model the agents use: a runtime setting, switchable from the dashboard.

``Settings`` is frozen, so the live value lives on ``app.state.model``; it is seeded from
``OCA_MODEL`` once and then persisted in the ``model`` setting. The next pipeline launch picks it up.
"""

from __future__ import annotations

import re

import httpx
from fastapi import FastAPI

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


def current_model(app: FastAPI) -> str:
    return getattr(app.state, "model", None) or app.state.settings.model


def set_model(app: FastAPI, name: str) -> str:
    name = (name or "").strip()
    if not MODEL_NAME.fullmatch(name):
        raise ValueError(f"Invalid model name {name!r}")
    app.state.model = name
    return name


async def list_models(ollama_url: str) -> list[str]:
    """Models installed in the local Ollama, or [] when it cannot be reached."""
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.get(ollama_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
            names = [str(m.get("name") or m.get("model") or "") for m in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return []
    return sorted(n for n in names if n)
