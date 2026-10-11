"""Deterministic health checks. No LLM involved."""

from __future__ import annotations

from oca.checks.base import HealthCheck, PlaceholderCheck
from oca.checks.endpoint_check import EndpointCheck
from oca.checks.log_error_parse import LogErrorParse
from oca.checks.metrics_check import MetricsCheck
from oca.checks.status_check import StatusCheck
from oca.config import Settings

PRIMARY_CHECK = "status_check"


def build_checks(settings: Settings, runner, db, integrations: dict | None = None) -> list[HealthCheck]:
    interval = settings.check_interval_s
    jira = (integrations or {}).get("jira")
    if jira is not None:
        from oca.integrations.jira import JiraReaderCheck

        jira_reader: HealthCheck = JiraReaderCheck(jira, interval_s=30)
    else:
        jira_reader = PlaceholderCheck("jira_ticket_reader", "jira", interval_s=30, enabled=False,
                                       detail="Jira integration not configured")
    return [
        StatusCheck(runner, interval_s=interval),
        EndpointCheck(runner, "orders-api", interval_s=interval),
        EndpointCheck(runner, "frontend", interval_s=interval, opens_ticket=False),
        MetricsCheck(runner, db, interval_s=max(interval, 15)),
        LogErrorParse(runner, interval_s=max(interval, 15)),
        jira_reader,
        PlaceholderCheck("slack_ticket_reader", "slack", interval_s=30, enabled=False,
                         detail="Slack integration not configured (stretch)"),
    ]


__all__ = ["HealthCheck", "PlaceholderCheck", "StatusCheck", "EndpointCheck", "MetricsCheck", "LogErrorParse",
           "build_checks", "PRIMARY_CHECK"]
