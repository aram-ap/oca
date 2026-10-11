"""Jira Cloud: tickets come in from Jira, progress goes back to the same issue (README §4.9, D8).

Inbound:  every ``poll_interval_s`` OCA searches Jira (by default: issues in ``project``
          labelled ``oca`` that are still To Do). Each new issue becomes one OCA ticket
          (source ``jira``) and the agents start. The same issue is never imported twice.
Outbound: OCA comments on the issue and moves it across the board as the ticket
          progresses. Incidents OCA detects itself get a new Jira issue
          (``create_on_detect``), labelled ``oca-auto`` so the poller never re-imports them.
Approval stays on the OCA dashboard; a Jira comment is never an instruction to OCA.

What goes to Jira: ticket ID, service, severity, status, time to fix and a dashboard
link (plus the Guardian's risk score at ``outbound_detail: summary``). Never titles,
logs, code, diffs, metrics or text written by agents.

Settings (dashboard, ``settings.jira``): ``enabled``, ``project``, ``jql``, ``poll_interval_s``,
``default_service``, ``create_on_detect``, ``issue_type``, ``status_map``, ``outbound_detail``.
Credentials (``.env``): ``OCA_JIRA_URL``, ``OCA_JIRA_EMAIL``, ``OCA_JIRA_API_TOKEN``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from oca.integrations.base import DeliveryError, Integration
from oca.integrations.slack import duration
from oca.schemas import (
    EventType, InboundTicket, OutboundDetail, OutboundMessage, Severity, Ticket, TicketEvent,
    TicketSource, TicketStatus,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

TIMEOUT_S = 15.0
PICKUP_LABEL = "oca"          # people add this label to hand a Jira issue to OCA
CREATED_LABEL = "oca-auto"    # issues OCA opens itself; never matched by the default search

# Which Jira status the issue moves to when the OCA ticket reaches a stage. Names must match
# the project's workflow (a standard Jira Software board: To Do, In Progress, In Review, Done).
DEFAULT_STATUS_MAP: dict[str, str] = {
    TicketStatus.investigating.value: "In Progress",
    TicketStatus.awaiting_approval.value: "In Review",
    TicketStatus.deploying.value: "In Progress",
    TicketStatus.resolved.value: "Done",
    TicketStatus.denied.value: "To Do",
    TicketStatus.rolled_back.value: "To Do",
    TicketStatus.failed.value: "To Do",
}

# Comments posted when the OCA ticket reaches a stage. {link} is the dashboard link.
STATUS_COMMENTS: dict[TicketStatus, str] = {
    TicketStatus.awaiting_approval: "Fix validated in the sandbox. Waiting for approval on the OCA dashboard: {link}",
    TicketStatus.deploying: "Deploying the fix.",
    TicketStatus.denied: "The fix was denied on the OCA dashboard. This needs a person: {link}",
    TicketStatus.rolled_back: "The deployment was rolled back. This needs a person: {link}",
    TicketStatus.failed: "OCA could not fix this. This needs a person: {link}",
}

PRIORITY_TO_SEVERITY = {"highest": Severity.high, "high": Severity.high, "medium": Severity.medium,
                        "low": Severity.low, "lowest": Severity.low}
SEVERITY_TO_PRIORITY = {Severity.critical: "Highest", Severity.high: "High",
                        Severity.medium: "Medium", Severity.low: "Low"}


def adf_to_text(node: Any) -> str:
    """Plain text from a Jira Cloud description (Atlassian Document Format) or a plain string."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    text = adf_to_text(node.get("content", []))
    if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock", "blockquote"}:
        text += "\n"
    return text


class JiraIntegration(Integration):
    name = "jira"
    defaults = {"enabled": False, "project": "", "jql": "", "poll_interval_s": 30, "default_service": "orders-api",
                "create_on_detect": True, "issue_type": "Task", "status_map": {},
                "outbound_detail": OutboundDetail.minimal.value}

    def __init__(self, app: "FastAPI", transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(app)
        self._transport = transport  # tests pass a fake Jira here

    # ------------------------------------------------------------------ configuration

    def has_credentials(self) -> bool:
        return not self.missing_credentials()

    def missing_credentials(self) -> list[str]:
        s = self.app.state.settings
        return [name for name, value in (("OCA_JIRA_URL", s.jira_url), ("OCA_JIRA_EMAIL", s.jira_email),
                                         ("OCA_JIRA_API_TOKEN", s.jira_api_token)) if not value]

    def enabled(self, cfg: dict[str, Any]) -> bool:
        return super().enabled(cfg) and bool(str(cfg.get("project") or "").strip() or str(cfg.get("jql") or "").strip())

    @staticmethod
    def jql(cfg: dict[str, Any]) -> str:
        if str(cfg.get("jql") or "").strip():
            return str(cfg["jql"]).strip()
        project = str(cfg.get("project") or "").strip().replace('"', "")
        return (f'project = "{project}" AND labels = "{PICKUP_LABEL}" '
                f'AND statusCategory = "To Do" ORDER BY created ASC')

    @staticmethod
    def status_for(cfg: dict[str, Any], stage: str) -> str | None:
        mapping = {**DEFAULT_STATUS_MAP, **(cfg.get("status_map") or {})}
        return mapping.get(stage) or None

    def issue_url(self, key: str) -> str:
        return f"{self.app.state.settings.jira_url}/browse/{key}"

    # --------------------------------------------------------------------- transport

    def _client(self) -> httpx.AsyncClient:
        s = self.app.state.settings
        return httpx.AsyncClient(base_url=s.jira_url, auth=(s.jira_email, s.jira_api_token), timeout=TIMEOUT_S,
                                 transport=self._transport, headers={"Accept": "application/json"})

    async def _call(self, client: httpx.AsyncClient, method: str, path: str,
                    body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> tuple[Any, int]:
        """One Jira request. Returns (parsed JSON or None, bytes sent)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            response = await client.request(method, path, content=data, params=params,
                                            headers={"Content-Type": "application/json"} if data else None)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"cannot reach Jira: {type(exc).__name__}") from None
        if response.status_code >= 400:
            try:
                detail = response.json()
                msg = "; ".join(list(detail.get("errorMessages") or []) +
                                [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()])
            except ValueError:
                msg = response.text[:200]
            retryable = response.status_code == 429 or response.status_code >= 500
            hint = {401: " (check OCA_JIRA_EMAIL / OCA_JIRA_API_TOKEN)", 403: " (the account lacks permission)",
                    404: " (check OCA_JIRA_URL, the project key or the issue key)"}.get(response.status_code, "")
            raise DeliveryError(f"Jira {method} {path}: HTTP {response.status_code} {msg}{hint}".strip(),
                                retryable=retryable)
        try:
            parsed = response.json() if response.content else None
        except ValueError:
            parsed = None
        return parsed, len(data or b"")

    # ----------------------------------------------------------------------- inbound

    async def poll(self) -> list[InboundTicket]:
        cfg = await self.config()
        async with self._client() as client:
            data, _ = await self._call(client, "GET", "/rest/api/3/search/jql", params={
                "jql": self.jql(cfg), "maxResults": 50,
                "fields": "summary,priority,components,description,labels"})
        out = []
        for issue in (data or {}).get("issues", []):
            fields = issue.get("fields") or {}
            if CREATED_LABEL in (fields.get("labels") or []):
                continue  # one of OCA's own issues
            priority = ((fields.get("priority") or {}).get("name") or "").lower()
            components = fields.get("components") or []
            out.append(InboundTicket(
                system="jira", key=issue["key"], url=self.issue_url(issue["key"]),
                title=str(fields.get("summary") or issue["key"]),
                description=adf_to_text(fields.get("description")).strip() or None,
                severity=PRIORITY_TO_SEVERITY.get(priority, Severity.medium),
                service=(components[0].get("name") if components else None) or cfg.get("default_service"),
                raw={"key": issue["key"]}))
        return out

    # ---------------------------------------------------------------------- outbound

    async def on_event(self, ticket: Ticket, event: TicketEvent) -> list[OutboundMessage]:
        cfg = await self.config()
        from_jira = ticket.source is TicketSource.jira
        link = f"[{ticket.id}|{self.dashboard_link(ticket.id)}]"
        msgs: list[OutboundMessage] = []

        def comment(text: str) -> None:
            msgs.append(self.message(ticket, "comment", {"body": f"*[OCA]* {text}"}))

        def move(stage: str) -> None:
            status = self.status_for(cfg, stage)
            if status:
                msgs.append(self.message(ticket, "transition", {"status": status}))

        if event.type is EventType.reported:
            if from_jira:
                comment(f"OCA picked this up as {ticket.id} and is investigating. Dashboard: {link}")
                move(TicketStatus.investigating.value)
            return msgs

        if event.type is EventType.detected:
            if ticket.source is TicketSource.health_check and cfg.get("create_on_detect") and not ticket.external_ref:
                fields: dict[str, Any] = {
                    "project": {"key": str(cfg.get("project") or "").strip()},
                    "issuetype": {"name": cfg.get("issue_type") or "Task"},
                    "summary": f"OCA {ticket.id}: {ticket.service} incident (severity {ticket.severity.value})",
                    "description": ("Opened automatically by OCA (On-Call Agent). Progress is posted here "
                                    f"as the incident is handled.\nDashboard: {link}"),
                    "labels": [CREATED_LABEL],
                    "priority": {"name": SEVERITY_TO_PRIORITY.get(ticket.severity, "High")},
                }
                msgs.append(self.message(ticket, "create_issue", {"fields": fields}))
            return msgs

        if not (from_jira or await self.app.state.db.get_link(ticket.id, self.name)
                or (ticket.source is TicketSource.health_check and cfg.get("create_on_detect"))):
            return msgs  # this ticket has no Jira issue

        if event.type is EventType.status_change:
            try:
                status = TicketStatus(event.payload.get("to"))
            except ValueError:
                return msgs
            if status is TicketStatus.resolved:
                return msgs  # the resolved event, which carries the time to fix, handles it
            if status in STATUS_COMMENTS:
                text = STATUS_COMMENTS[status].format(link=link)
                if status is TicketStatus.awaiting_approval and self.detail(cfg) is OutboundDetail.summary:
                    review = await self.app.state.db.latest_event(ticket.id, EventType.review)
                    risk = ((review.payload.get("review") or {}) if review else {}).get("risk_score")
                    if isinstance(risk, int):
                        text += f" Risk score: {risk}/100."
                comment(text)
            move(status.value)
            return msgs

        if event.type is EventType.resolved:
            mttr = event.payload.get("mttr") or {}
            parts = [f"{label} {duration(mttr[key])}" for key, label in (
                ("detection_s", "detection"), ("diagnosis_s", "diagnosis"), ("fix_s", "fix"),
                ("validation_s", "validation"), ("deployment_s", "deployment")) if mttr.get(key) is not None]
            text = f"Resolved by OCA in {duration(mttr.get('total_s'))}."
            if parts:
                text += " (" + ", ".join(parts) + ")"
            comment(f"{text} Timeline: {link}")
            move(TicketStatus.resolved.value)
        return msgs

    async def deliver(self, item: dict[str, Any]) -> int:
        kind, payload, ticket_id = item["kind"], item["payload"], item["ticket_id"]
        async with self._client() as client:
            if kind == "create_issue":
                return await self._create(client, ticket_id, payload["fields"])
            key = await self._issue_key(ticket_id)
            if key is None:
                raise DeliveryError(f"{ticket_id} has no Jira issue", retryable=False)
            if kind == "comment":
                _, sent = await self._call(client, "POST", f"/rest/api/2/issue/{key}/comment", {"body": payload["body"]})
                return sent
            if kind == "transition":
                return await self._transition(client, key, payload["status"])
        raise DeliveryError(f"unknown Jira message kind {kind!r}", retryable=False)

    async def _issue_key(self, ticket_id: str) -> str | None:
        db = self.app.state.db
        ticket = await db.get_ticket(ticket_id)
        ref = (ticket.external_ref or {}) if ticket else {}
        if ref.get("system") == self.name and ref.get("key"):
            return str(ref["key"])
        link = await db.get_link(ticket_id, self.name)
        return str(link["key"]) if link and link.get("key") else None

    async def _create(self, client: httpx.AsyncClient, ticket_id: str, fields: dict[str, Any]) -> int:
        try:
            data, sent = await self._call(client, "POST", "/rest/api/2/issue", {"fields": fields})
        except DeliveryError as exc:
            if "priority" not in str(exc).lower() or "priority" not in fields:
                raise
            fields = {k: v for k, v in fields.items() if k != "priority"}  # priority not on this project's screen
            data, sent = await self._call(client, "POST", "/rest/api/2/issue", {"fields": fields})
        key = (data or {}).get("key")
        if not key:
            raise DeliveryError("Jira did not return the new issue key", retryable=False)
        db = self.app.state.db
        await db.set_link(ticket_id, self.name, {"key": key, "url": self.issue_url(key)})
        ticket = await db.get_ticket(ticket_id)
        if ticket is not None and not ticket.external_ref:
            await db.update_ticket(ticket_id, external_ref={"system": self.name, "key": key,
                                                             "url": self.issue_url(key)})
        return sent

    async def _transition(self, client: httpx.AsyncClient, key: str, status: str) -> int:
        issue, _ = await self._call(client, "GET", f"/rest/api/2/issue/{key}", params={"fields": "status"})
        current = (((issue or {}).get("fields") or {}).get("status") or {}).get("name", "")
        if current.lower() == status.lower():
            return 0
        options = ((await self._call(client, "GET", f"/rest/api/2/issue/{key}/transitions"))[0] or {}).get(
            "transitions", [])
        match = next((t for t in options if (t.get("to") or {}).get("name", "").lower() == status.lower()), None) or \
            next((t for t in options if t.get("name", "").lower() == status.lower()), None)
        if match is None:
            available = ", ".join(f"'{t.get('name')}' -> {(t.get('to') or {}).get('name')}" for t in options) or "none"
            raise DeliveryError(f"{key}: no transition from '{current}' to '{status}'. Available: {available}. "
                                f"Adjust status_map in the Jira settings.", retryable=False)
        _, sent = await self._call(client, "POST", f"/rest/api/2/issue/{key}/transitions",
                                   {"transition": {"id": match["id"]}})
        return sent

    # ------------------------------------------------------------------ connection test

    async def check(self) -> dict[str, Any]:
        """Log in, find the project, and confirm every status in the status map exists on its board."""
        missing = self.missing_credentials()
        if missing:
            return {"ok": False, "detail": f"Set {', '.join(missing)} in .env and restart OCA.", "lines": []}
        cfg = await self.config()
        project = str(cfg.get("project") or "").strip()
        if not project:
            return {"ok": False, "detail": "Set the Jira project key in Settings first.", "lines": []}
        lines: list[str] = []
        try:
            async with self._client() as client:
                me, _ = await self._call(client, "GET", "/rest/api/2/myself")
                lines.append(f"ok  logged in as {(me or {}).get('displayName', 'unknown')}")
                proj, _ = await self._call(client, "GET", f"/rest/api/2/project/{project}")
                lines.append(f"ok  project {(proj or {}).get('key', project)}: {(proj or {}).get('name', '')}")
                types, _ = await self._call(client, "GET", f"/rest/api/2/project/{project}/statuses")
        except DeliveryError as exc:
            return {"ok": False, "detail": str(exc), "lines": lines}
        by_type = {t.get("name"): {st.get("name") for st in t.get("statuses", [])} for t in types or []}
        issue_type = cfg.get("issue_type") or "Task"
        if issue_type not in by_type:
            lines.append(f"ERR issue type '{issue_type}' is not in the project; use one of: {', '.join(by_type)}")
            return {"ok": False, "detail": "Issue type not found.", "lines": lines}
        board = by_type[issue_type]
        lines.append(f"ok  issue type '{issue_type}'; board columns: {', '.join(sorted(board))}")
        ok = True
        for stage, status in {**DEFAULT_STATUS_MAP, **(cfg.get("status_map") or {})}.items():
            found = status in board
            ok = ok and found
            lines.append(f"{'ok ' if found else 'ERR'} {stage} -> '{status}'")
        detail = "Connected; the board matches the status map." if ok else \
            "Connected, but some statuses are not on the board; fix the status map in Settings."
        return {"ok": ok, "detail": detail, "lines": lines}
