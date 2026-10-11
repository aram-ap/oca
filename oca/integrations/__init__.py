"""Ticketing integrations (Jira, Slack). See README §4.9 and decision D8.

Each integration stays idle until it is switched on in settings (``settings.jira`` /
``settings.slack``) and its credentials are set in ``.env``.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from oca.integrations.base import DeliveryError, Integration
from oca.integrations.jira import JiraIntegration
from oca.integrations.slack import SlackIntegration
from oca.integrations.worker import IntegrationWorker


def build_integrations(app: FastAPI) -> list[Integration]:
    """Every integration OCA knows about."""
    return [JiraIntegration(app), SlackIntegration(app)]


def integration_worker(app: FastAPI) -> IntegrationWorker:
    """The running worker, or an idle one (for status and connection tests) when it is not started."""
    worker = getattr(app.state, "integrations", None)
    return worker if worker is not None else IntegrationWorker(app, build_integrations(app))


async def start_integrations(app: FastAPI) -> asyncio.Task:
    worker = IntegrationWorker(app, build_integrations(app))
    app.state.integrations = worker
    return await worker.start()


__all__ = ["DeliveryError", "Integration", "IntegrationWorker", "JiraIntegration", "SlackIntegration",
           "build_integrations", "integration_worker", "start_integrations"]
