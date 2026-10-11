from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from oca.auth import Principal, session_principal

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _fmt_ts(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    return "" if value is None else str(value)


def _fmt_dt(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return "" if value is None else str(value)


def _duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _pretty(value) -> str:
    return json.dumps(value, indent=2, default=str)


TEMPLATES.env.filters.update({"ts": _fmt_ts, "dt": _fmt_dt, "duration": _duration, "pretty": _pretty})


def web_user(request: Request) -> Principal:
    principal = session_principal(request, request.app.state.settings)
    if principal is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return principal


def is_fragment(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def render(request: Request, template: str, **context):
    settings = request.app.state.settings
    context.setdefault("settings", settings)
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("path", request.url.path)
    return TEMPLATES.TemplateResponse(request, template, context)
