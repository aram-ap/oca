"""Dashboard time zone: one validated IANA zone kept on ``app.state`` and in the ``timezone`` setting."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COMMON_TIMEZONES = ("UTC", "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York",
                    "America/Sao_Paulo", "Europe/London", "Europe/Berlin", "Europe/Paris", "Africa/Johannesburg",
                    "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney")


def zone(name: str) -> ZoneInfo:
    """Validate an IANA zone name; raises ValueError."""
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"{name!r} is not an IANA time zone (e.g. America/Los_Angeles)")


def set_timezone(app, name: str) -> ZoneInfo:
    app.state.tz = zone(name)
    app.state.tz_name = str(name)
    return app.state.tz
