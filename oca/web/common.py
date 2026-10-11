from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from oca.auth import Principal, session_principal
from oca.tz import COMMON_TIMEZONES, set_timezone  # noqa: F401 - re-exported for the web layer

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _localize(ctx, value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    request = ctx.get("request") if ctx is not None else None
    tz = getattr(getattr(request, "app", None), "state", None)
    tz = getattr(tz, "tz", None) or timezone.utc
    return value.astimezone(tz)


@pass_context
def _fmt_ts(ctx, value) -> str:
    value = _localize(ctx, value)
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    return "" if value is None else str(value)


@pass_context
def _fmt_dt(ctx, value) -> str:
    value = _localize(ctx, value)
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
    context.setdefault("tz_name", getattr(request.app.state, "tz_name", "UTC"))
    return TEMPLATES.TemplateResponse(request, template, context)
