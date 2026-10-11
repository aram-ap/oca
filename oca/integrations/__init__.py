"""Ticketing integrations (Jira, Slack): configuration, lifecycle, and the outbox pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI

from oca.integrations.base import IntegrationError, TicketingIntegration, mask_secrets, merge_secrets
from oca.integrations.jira import READER_CHECK, JiraIntegration
from oca.integrations.outbox import EventRelay, OutboxWorker
from oca.integrations.slack import SlackIntegration

log = logging.getLogger("oca.integrations")

__all__ = ["IntegrationError", "TicketingIntegration", "JiraIntegration", "SlackIntegration", "EventRelay", "OutboxWorker",
           "build_integrations", "start_integrations", "start_outbox", "apply_settings", "save_integration_settings",
           "integration_status", "mask_secrets", "merge_secrets"]


def build_integrations(app: FastAPI) -> dict[str, TicketingIntegration]:
    return {"jira": JiraIntegration(app), "slack": SlackIntegration(app)}


async def seed_from_env(app: FastAPI) -> None:
    """Environment variables fill in connection settings; values saved on the settings page win afterwards."""
    settings, db = app.state.settings, app.state.db
    env_jira = {k: v for k, v in (("base_url", settings.jira_url), ("email", settings.jira_email),
                                  ("token", settings.jira_token), ("project", settings.jira_project)) if v}
    if env_jira:
        stored = await db.get_setting("jira", {}) or {}
        fresh = not stored.get("base_url") and not stored.get("token")
        if any(not stored.get(k) for k in env_jira):
            merged = {**stored, **{k: v for k, v in env_jira.items() if not stored.get(k)}}
            if fresh:
                merged["enabled"] = True
            await db.set_setting("jira", merged)
    if settings.slack_webhook_url:
        stored = await db.get_setting("slack", {}) or {}
        if not stored.get("webhook_url"):
            await db.set_setting("slack", {**stored, "webhook_url": settings.slack_webhook_url, "enabled": True})


async def apply_settings(app: FastAPI) -> None:
    """Turn the Jira reader check on or off to match the stored configuration."""
    jira = app.state.integrations.get("jira")
    if jira is None:
        return
    config = await jira.config()
    enabled = bool(config.get("enabled")) and jira.configured(config) and bool((config.get("jql") or "").strip())
    try:
        interval = max(5, int(config.get("poll_interval_s") or 30))
    except (TypeError, ValueError):
        interval = 30
    if await app.state.db.get_check(READER_CHECK) is not None:
        await app.state.db.update_check(READER_CHECK, enabled=enabled, interval_s=interval)


async def save_integration_settings(app: FastAPI, name: str, changes: dict[str, Any]) -> dict[str, Any]:
    integration = app.state.integrations.get(name)
    if integration is None:
        raise KeyError(name)
    stored = await integration.config()
    merged = merge_secrets(name, changes, stored)
    merged = {k: v for k, v in merged.items() if k in integration.defaults}
    await app.state.db.set_setting(name, merged)
    await apply_settings(app)
    return mask_secrets(name, merged)


async def integration_status(app: FastAPI) -> dict[str, Any]:
    db = app.state.db
    stats = await db.outbox_stats()
    out: dict[str, Any] = {}
    for name, integration in app.state.integrations.items():
        config = await integration.config()
        out[name] = {"config": mask_secrets(name, config), "configured": integration.configured(config),
                     "enabled": bool(config.get("enabled")) and integration.configured(config),
                     "outbox": stats.get(name, {"pending": 0, "failed": 0, "bytes_sent": 0})}
    reader = await db.get_check(READER_CHECK)
    if reader is not None:
        out["jira"]["reader"] = {"enabled": reader.enabled, "interval_s": reader.interval_s,
                                 "last_ok": reader.last_ok, "last_detail": reader.last_result.detail if reader.last_result else None}
    out["recent"] = await db.list_outbox(limit=20)
    return out


async def start_integrations(app: FastAPI) -> None:
    """Runs before the scheduler so the Jira reader check can be built around the integration."""
    app.state.integrations = build_integrations(app)
    await seed_from_env(app)


async def start_outbox(app: FastAPI) -> asyncio.Task:
    """Runs after the scheduler has registered its checks: applies settings, then relays and delivers forever."""
    await apply_settings(app)
    integrations = app.state.integrations
    app.state.relay = EventRelay(app, integrations)
    app.state.outbox = OutboxWorker(app, integrations, poll_s=app.state.settings.outbox_poll_s)

    async def run() -> None:
        await asyncio.gather(app.state.relay.run_forever(), app.state.outbox.run_forever())

    return asyncio.create_task(run(), name="oca-integrations")
