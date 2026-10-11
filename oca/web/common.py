from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from oca.auth import Principal, session_principal
from oca.tz import COMMON_TIMEZONES, set_timezone  # noqa: F401 - re-exported for the web layer

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def static_version() -> str:
    """Short fingerprint of the static assets, appended to their URLs as ?v=... so browsers never keep
    serving a stale pager.js / dashboard.js after a deploy. Based on file names, sizes and mtimes, so it
    changes on a plain git pull without a server restart."""
    h = hashlib.sha1()
    for f in sorted(STATIC_DIR.glob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:10]


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


def _diff_html(text) -> Markup:
    """A unified diff as a <pre> with one coloured span per line (content is escaped)."""
    rows = []
    for line in str(text or "").splitlines():
        if line.startswith(("+++", "---", "diff ", "index ")):
            cls = "file"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        rows.append(f'<span class="d-{cls}">{escape(line)}</span>')
    return Markup('<pre class="diff">' + "\n".join(rows) + "</pre>")


TEMPLATES.env.filters.update({"ts": _fmt_ts, "dt": _fmt_dt, "duration": _duration, "pretty": _pretty, "diff_html": _diff_html})


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
    context.setdefault("static_version", static_version())
    context.setdefault("tz_name", getattr(request.app.state, "tz_name", "UTC"))
    return TEMPLATES.TemplateResponse(request, template, context)
