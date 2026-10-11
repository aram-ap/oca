"""Jira (Cloud or Data Center) ticketing integration over REST API v2 with httpx.

Outbound: create an issue when a ticket is detected, comment on milestones, and move the
issue through the workflow via a configurable status map. Inbound: poll a JQL query and
open local tickets for new issues. Credentials live in the ``jira`` settings row.

    python3 -m oca.integrations.jira --doctor     check OCA_JIRA_* credentials from the shell
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

import httpx

from oca.checks.base import HealthCheck
from oca.integrations.base import DeferDelivery, IntegrationError, TicketingIntegration
from oca.schemas import (
    Actor, CheckResult, EventType, InboundTicket, OutboundMessage, Severity, Ticket, TicketCreate, TicketEvent,
    TicketEventCreate, TicketSource, TicketStatus,
)

log = logging.getLogger("oca.integrations.jira")

LABEL = "oca"
READER_CHECK = "jira_ticket_reader"
# OCA status -> Jira status name. The defaults fit a Jira Software project's stock workflow.
DEFAULT_STATUS_MAP: dict[str, str] = {
    TicketStatus.detected.value: "To Do",
    **{s.value: "In Progress" for s in (TicketStatus.investigating, TicketStatus.fix_proposed, TicketStatus.sandbox_validated,
                                        TicketStatus.reviewed, TicketStatus.gate, TicketStatus.awaiting_approval,
                                        TicketStatus.approved, TicketStatus.deploying, TicketStatus.awaiting_verification)},
    TicketStatus.resolved.value: "Done",
    TicketStatus.denied.value: "To Do",
    TicketStatus.rolled_back.value: "To Do",
    TicketStatus.failed.value: "To Do",
}
PRIORITY_FOR = {Severity.critical: "Highest", Severity.high: "High", Severity.medium: "Medium", Severity.low: "Low"}
SEVERITY_FOR = {"highest": Severity.high, "high": Severity.high, "medium": Severity.medium,
                "low": Severity.low, "lowest": Severity.low}
JIRA_DEFAULTS: dict[str, Any] = {
    "enabled": False, "base_url": "", "email": "", "token": "", "project": "", "issue_type": "Task",
    "deployment": "cloud", "outbound_detail": "minimal", "create_on_detect": True, "status_map": {},
    "jql": "", "poll_interval_s": 30, "default_service": "orders-api",
}


def stage_label(status: str) -> str:
    return f"{LABEL}-{status.replace('_', '-')}"


class JiraClient:
    """Thin async REST client. Counts the bytes it sends so the LOCAL AI panel can show them."""

    def __init__(self, base_url: str, email: str, token: str, deployment: str = "cloud",
                 timeout_s: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.deployment = deployment
        self.timeout_s = timeout_s
        self.transport = transport
        self.bytes_sent = 0
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    async def call(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False, transport=self.transport) as client:
                response = await client.request(method, self.base_url + path, content=data, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"cannot reach {self.base_url}: {type(exc).__name__}: {exc}") from exc
        self.bytes_sent += len(data or b"")
        if response.status_code >= 400:
            try:
                detail = response.json()
                message = "; ".join(list(detail.get("errorMessages", [])) +
                                    [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()])
            except ValueError:
                message = response.text[:200]
            hint = {401: " (check email / API token)", 403: " (the account lacks permission)",
                    404: " (check base URL, project key or issue key)"}.get(response.status_code, "")
            raise IntegrationError(f"{method} {path}: HTTP {response.status_code} {message}{hint}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(f"{method} {path}: invalid JSON from Jira") from exc

    # --------------------------------------------------------------- endpoints

    async def myself(self) -> dict[str, Any]:
        return await self.call("GET", "/rest/api/2/myself")

    async def project(self, key: str) -> dict[str, Any]:
        return await self.call("GET", f"/rest/api/2/project/{key}")

    async def project_statuses(self, key: str) -> list[dict[str, Any]]:
        return await self.call("GET", f"/rest/api/2/project/{key}/statuses") or []

    async def create_issue(self, fields: dict[str, Any]) -> str:
        try:
            created = await self.call("POST", "/rest/api/2/issue", {"fields": fields})
        except IntegrationError as exc:
            if "priority" not in str(exc).lower() or "priority" not in fields:
                raise
            fields = {k: v for k, v in fields.items() if k != "priority"}  # not on this project's create screen
            created = await self.call("POST", "/rest/api/2/issue", {"fields": fields})
        return created["key"]

    async def issue(self, key: str, fields: str) -> dict[str, Any]:
        return await self.call("GET", f"/rest/api/2/issue/{key}", params={"fields": fields})

    async def comment(self, key: str, body: str) -> None:
        await self.call("POST", f"/rest/api/2/issue/{key}/comment", {"body": body})

    async def transition(self, key: str, status: str) -> bool:
        """Move the issue to ``status`` if it is not there already. Returns True when a transition ran."""
        current = (await self.issue(key, "status"))["fields"]["status"]["name"]
        if current.lower() == status.lower():
            return False
        options = (await self.call("GET", f"/rest/api/2/issue/{key}/transitions"))["transitions"]
        match = next((t for t in options if t["to"]["name"].lower() == status.lower()), None) or \
            next((t for t in options if t["name"].lower() == status.lower()), None)
        if match is None:
            available = ", ".join(f"'{t['name']}' -> {t['to']['name']}" for t in options) or "none"
            raise IntegrationError(f"{key}: no transition from '{current}' to '{status}' (available: {available}); "
                                   "adjust the status map")
        await self.call("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        return True

    async def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        ops = [{"remove": label} for label in remove] + [{"add": label} for label in add]
        await self.call("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": ops}})

    async def search(self, jql: str, fields: str, max_results: int = 50) -> list[dict[str, Any]]:
        if self.deployment == "data_center":
            data = await self.call("GET", "/rest/api/2/search", params={"jql": jql, "fields": fields, "maxResults": max_results})
        else:
            data = await self.call("GET", "/rest/api/3/search/jql", params={"jql": jql, "fields": fields, "maxResults": max_results})
        return list((data or {}).get("issues", []))

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"


class JiraIntegration(TicketingIntegration):
    name = "jira"
    defaults = JIRA_DEFAULTS

    def __init__(self, app) -> None:
        super().__init__(app)
        self.transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport here

    def configured(self, config: dict[str, Any]) -> bool:
        return all(str(config.get(k, "")).strip() for k in ("base_url", "email", "token", "project"))

    def client(self, config: dict[str, Any]) -> JiraClient:
        return JiraClient(config["base_url"], config["email"], config["token"], config.get("deployment", "cloud"),
                          transport=self.transport)

    @staticmethod
    def status_map(config: dict[str, Any]) -> dict[str, str]:
        custom = config.get("status_map") or {}
        return {**DEFAULT_STATUS_MAP, **{str(k): str(v) for k, v in custom.items() if v}}

    @staticmethod
    def external_key(ticket: Ticket) -> str | None:
        ref = ticket.external_ref or {}
        return ref.get("key") if ref.get("system") == "jira" else None

    # ----------------------------------------------------------------- outbound

    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]:
        config = await self.config()
        detail = self.detail_of(config)
        update = await self.describe(ticket, event, detail)
        if update is None:
            return []
        if update.kind != "opened" and self.external_key(ticket) is None \
                and not await self.db.has_pending_outbox(ticket.id, self.name, "create_issue"):
            return []  # the ticket pre-dates the Jira connection (or creation was turned off): nothing to update
        out: list[OutboundMessage] = []
        if update.kind == "opened":
            if not config.get("create_on_detect", True) or ticket.source is TicketSource.jira:
                return []
            fields = {"project": {"key": config["project"]}, "issuetype": {"name": config.get("issue_type") or "Task"},
                      "summary": f"[{ticket.id}] {ticket.title}"[:250],
                      "description": f"{update.text}\n\nOpened by OCA (local on-call agent). Approve or deny the fix on the "
                                     f"OCA dashboard; comments here are never read as instructions.",
                      "labels": [LABEL, ticket.service, stage_label(ticket.status.value)],
                      "priority": {"name": PRIORITY_FOR.get(ticket.severity, "Medium")}}
            return [OutboundMessage(target=self.name, kind="create_issue", payload={"fields": fields}, ticket_id=ticket.id)]
        if update.status is not None:
            out.append(OutboundMessage(target=self.name, kind="transition", ticket_id=ticket.id, payload={
                "status": self.status_map(config).get(update.status.value, "In Progress"),
                "label": stage_label(update.status.value)}))
        if update.text:
            out.append(OutboundMessage(target=self.name, kind="comment", ticket_id=ticket.id,
                                       payload={"body": f"*[OCA]* {update.text}"}))
        return out

    async def deliver(self, item: dict[str, Any]) -> int:
        config = await self.config()
        if not self.configured(config):
            raise IntegrationError("Jira is not configured")
        ticket = await self.db.get_ticket(item["ticket_id"])
        if ticket is None:
            raise IntegrationError(f"unknown ticket {item['ticket_id']}")
        client = self.client(config)
        kind, payload = item["kind"], item["payload"]
        key = self.external_key(ticket)
        if kind == "create_issue":
            if key is not None:
                return 0  # already linked (inbound ticket or a retried create)
            key = await client.create_issue(payload["fields"])
            await self.db.update_ticket(ticket.id, external_ref={"system": "jira", "key": key, "url": client.browse_url(key)})
            await self.record_sync(ticket.id, kind, client.bytes_sent, key=key, url=client.browse_url(key))
            return client.bytes_sent
        if key is None:
            if await self.db.has_pending_outbox(ticket.id, self.name, "create_issue"):
                raise DeferDelivery("waiting for the Jira issue to be created")
            raise IntegrationError(f"{ticket.id} has no Jira issue to update")
        if kind == "comment":
            await client.comment(key, payload["body"])
        elif kind == "transition":
            moved = await client.transition(key, payload["status"])
            await client.set_labels(key, add=[payload["label"]],
                                    remove=[stage_label(s.value) for s in TicketStatus if stage_label(s.value) != payload["label"]])
            await self.record_sync(ticket.id, kind, client.bytes_sent, key=key, status=payload["status"], moved=moved)
            return client.bytes_sent
        else:
            raise IntegrationError(f"unknown outbox kind {kind!r}")
        await self.record_sync(ticket.id, kind, client.bytes_sent, key=key)
        return client.bytes_sent

    # ------------------------------------------------------------------ inbound

    async def poll(self) -> list[InboundTicket]:
        config = await self.config()
        jql = (config.get("jql") or "").strip()
        if not self.configured(config) or not jql:
            return []
        client = self.client(config)
        created: list[InboundTicket] = []
        for hit in await client.search(jql, "summary,status,priority,components,labels"):
            key = hit.get("key")
            if not key or await self.db.ticket_by_external_ref("jira", key) is not None:
                continue
            issue = await client.issue(key, "summary,description,priority,components,labels")
            fields = issue.get("fields") or {}
            priority = ((fields.get("priority") or {}).get("name") or "").lower()
            components = fields.get("components") or []
            inbound = InboundTicket(
                system="jira", key=key, url=client.browse_url(key), title=str(fields.get("summary") or key)[:200],
                description=fields.get("description") if isinstance(fields.get("description"), str) else None,
                severity=SEVERITY_FOR.get(priority, Severity.medium),
                service=(components[0].get("name") if components else None) or config.get("default_service") or "orders-api",
                raw={"labels": fields.get("labels", [])})
            await self.open_ticket(inbound)
            created.append(inbound)
        return created

    async def open_ticket(self, inbound: InboundTicket) -> Ticket:
        ticket = await self.db.create_ticket(
            TicketCreate(source=TicketSource.jira, service=inbound.service or "orders-api", title=inbound.title,
                         severity=inbound.severity, description=inbound.description,
                         external_ref={"system": "jira", "key": inbound.key, "url": inbound.url}),
            TicketEventCreate(actor=Actor.jira, type=EventType.reported,
                              payload={"description": inbound.description or "", "title": inbound.title,
                                       "external_ref": {"system": "jira", "key": inbound.key, "url": inbound.url}}))
        log.info("opened %s from Jira %s", ticket.id, inbound.key)
        pipeline = getattr(self.app.state, "pipeline", None)
        if pipeline is not None and self.settings.autostart_pipeline:
            pipeline.launch(ticket.id)
        return ticket

    # ------------------------------------------------------------------- doctor

    async def test(self) -> dict[str, Any]:
        config = await self.config()
        if not self.configured(config):
            raise IntegrationError("base URL, email, API token and project key are all required")
        client = self.client(config)
        me = await client.myself()
        project = await client.project(config["project"])
        issue_types = [t.get("name") for t in project.get("issueTypes", [])]
        statuses: list[str] = []
        for entry in await client.project_statuses(config["project"]):
            if entry.get("name") == (config.get("issue_type") or "Task") or not statuses:
                statuses = [s.get("name") for s in entry.get("statuses", [])]
        result = {"ok": True, "user": me.get("displayName") or me.get("emailAddress"),
                  "project": project.get("name"), "issue_types": issue_types, "statuses": statuses,
                  "issue_type_ok": (config.get("issue_type") or "Task") in issue_types,
                  "status_map": self.status_map(config)}
        mapped = {v for v in result["status_map"].values()}
        result["unmapped_statuses"] = sorted(mapped - set(statuses)) if statuses else []
        if (config.get("jql") or "").strip():
            result["jql_matches"] = len(await client.search(config["jql"], "summary", 50))
        return result


class JiraReaderCheck(HealthCheck):
    """Polls Jira for new issues on the scheduler; it never opens tickets through the health path."""

    name = READER_CHECK
    service = "jira"
    opens_ticket = False
    enabled_by_default = False

    def __init__(self, integration: JiraIntegration, interval_s: int = 30) -> None:
        self.integration = integration
        self.interval_s = interval_s

    async def run(self) -> CheckResult:
        config = await self.integration.config()
        if not self.integration.configured(config):
            return CheckResult(ok=True, check=self.name, service=self.service, detail="Jira is not configured")
        if not (config.get("jql") or "").strip():
            return CheckResult(ok=True, check=self.name, service=self.service, detail="No inbound JQL configured; outbound only")
        try:
            created = await self.integration.poll()
        except IntegrationError as exc:
            return CheckResult(ok=False, check=self.name, service=self.service, detail=f"poll failed: {exc}"[:300],
                               breaches=["check_error"])
        detail = f"{len(created)} new issue(s): " + ", ".join(t.key for t in created) if created else "no new issues"
        return CheckResult(ok=True, check=self.name, service=self.service, detail=detail)


async def _doctor() -> int:
    env = {k: os.getenv(f"OCA_JIRA_{k.upper()}", "") for k in ("url", "email", "token", "project")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("missing: " + ", ".join(f"OCA_JIRA_{k.upper()}" for k in missing), file=sys.stderr)
        return 2
    client = JiraClient(env["url"], env["email"], env["token"], os.getenv("OCA_JIRA_DEPLOYMENT", "cloud"))
    try:
        me = await client.myself()
        project = await client.project(env["project"])
        print(f"user: {me.get('displayName')} <{me.get('emailAddress')}>")
        print(f"project: {project.get('name')} ({env['project']}); issue types: "
              + ", ".join(t.get('name', '?') for t in project.get('issueTypes', [])))
        for entry in await client.project_statuses(env["project"]):
            print(f"statuses for {entry.get('name')}: " + ", ".join(s.get('name', '?') for s in entry.get('statuses', [])))
    except IntegrationError as exc:
        print(f"doctor failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doctor", action="store_true", help="Check OCA_JIRA_URL/EMAIL/TOKEN/PROJECT against Jira")
    args = parser.parse_args()
    if not args.doctor:
        parser.error("Use --doctor")
    raise SystemExit(asyncio.run(_doctor()))
