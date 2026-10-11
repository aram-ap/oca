"""Incident reports: a post-incident write-up built deterministically from the ticket's own events.

One report is written whenever a ticket reaches a terminal state (resolved, failed, rolled back).
No LLM is involved in composing it: agent text (the Investigator's diagnosis, the Guardian's
justification) is quoted as written, and every number comes from server-measured events.
The Markdown file lives under ``OCA_DATA_DIR/reports/<ticket>.md``; a ``report`` event on the
ticket carries its dashboard URL so Jira and Slack can link to it (the content stays local, D8).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from oca.integrations.base import duration
from oca.schemas import (
    TERMINAL_STATUSES, Actor, Deployment, EventType, MTTR, Ticket, TicketEvent, TicketEventCreate, TicketStatus,
)

log = logging.getLogger("oca.reports")

WRITE_TOOLS = ("set_config", "create_migration", "apply_patch")
TIMELINE_STAGES = ("snapshot", "canary", "widen", "full", "verify", "monitor", "auto_restore")
REPORT_DELAY_S = 1.0  # terminal status_change and its `resolved`/`rolled_back` event arrive back to back


# ------------------------------------------------------------------------- helpers


def _localize(value: datetime, tz) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz or timezone.utc)


def _ts(value: datetime | None, tz) -> str:
    return _localize(value, tz).strftime("%H:%M:%S") if value else "—"


def _dt(value: datetime | None, tz) -> str:
    return _localize(value, tz).strftime("%Y-%m-%d %H:%M:%S %Z") if value else "—"


def _pct(value: Any) -> str:
    return f"{float(value):.1f}%" if isinstance(value, (int, float)) else "—"


def _ms(value: Any) -> str:
    return f"{float(value):.0f} ms" if isinstance(value, (int, float)) else "—"


def _conf(value: Any) -> str:
    return f"{float(value):.0%}" if isinstance(value, (int, float)) else "n/a"


def _first(events: list[TicketEvent], type_: EventType) -> TicketEvent | None:
    return next((e for e in events if e.type is type_), None)


def _last(events: list[TicketEvent], type_: EventType) -> TicketEvent | None:
    return next((e for e in reversed(events) if e.type is type_), None)


_CONFIG_LINE = re.compile(r"^\+([A-Z][A-Z0-9_]*)=(\S+)$", re.M)


def config_change(payload: dict[str, Any]) -> str:
    """``KEY=value`` for a set_config finding; older events lack the fields, so fall back to the diff."""
    if payload.get("key") is not None:
        return f"{payload['key']}={payload.get('value')}"
    m = _CONFIG_LINE.search(payload.get("diff") or "")
    return f"{m.group(1)}={m.group(2)}" if m else "config"


def summary_line(ticket: Ticket, events: list[TicketEvent]) -> str:
    """One sentence for the Jira/Slack message at summary detail: outcome plus the fix, no agent prose."""
    fixes = [e for e in events if e.type is EventType.finding and e.payload.get("tool") in WRITE_TOOLS and e.payload.get("applied")]
    fix = ""
    if fixes:
        p = fixes[-1].payload
        if p.get("tool") == "set_config":
            fix = f" Fix: {config_change(p)}."
        else:
            fix = f" Fix: {p.get('tool')} on {', '.join(p.get('files_changed') or [])}."
    if ticket.status is TicketStatus.resolved:
        return f"{ticket.id} resolved for {ticket.service}.{fix}"
    if ticket.status is TicketStatus.rolled_back:
        return f"{ticket.id}: the fix for {ticket.service} was rolled back; a human is needed.{fix}"
    return f"{ticket.id}: OCA could not produce a validated fix for {ticket.service}; a human is needed."


# -------------------------------------------------------------------------- builder


def build_report(ticket: Ticket, events: list[TicketEvent], mttr: MTTR | None, deployment: Deployment | None,
                 tz=None, public_url: str = "") -> str:
    lines: list[str] = []
    add = lines.append
    detected = _first(events, EventType.detected) or _first(events, EventType.reported)
    diagnosis = _last(events, EventType.diagnosis)
    review = _last(events, EventType.review)
    gate = _last(events, EventType.gate_decision)
    human = _last(events, EventType.human_decision)
    rolled_back = _last(events, EventType.rolled_back)
    befores = [e for e in events if e.type is EventType.sandbox_result and e.payload.get("phase") == "before"]
    afters = [e for e in events if e.type is EventType.sandbox_result and e.payload.get("phase") == "after"]
    fixes = [e for e in events if e.type is EventType.finding and e.payload.get("tool") in WRITE_TOOLS and e.payload.get("applied")]
    errors = [e for e in events if e.type is EventType.error]
    attempts = max([int(e.payload.get("attempt") or 0) for e in events if e.type is EventType.status_change] + [ticket.attempt, 1])
    link = f"{public_url.rstrip('/')}/tickets/{ticket.id}" if public_url else ""

    # ---- header
    add(f"# Incident report {ticket.id} — {ticket.title}")
    add("")
    add(f"**Status:** {ticket.status.value.replace('_', ' ')} · **Service:** {ticket.service} · "
        f"**Severity:** {ticket.severity.value} · **Source:** {ticket.source.value}")
    add(f"**Opened:** {_dt(ticket.created_at, tz)} · **Closed:** {_dt(ticket.resolved_at or ticket.updated_at, tz)}"
        + (f" · **MTTR:** {duration(mttr.total_s)}" if mttr and mttr.total_s is not None else ""))
    ref = ticket.external_ref or {}
    if ref.get("key"):
        add(f"**External issue:** [{ref.get('system', 'external').capitalize()} {ref['key']}]({ref.get('url', '')})")
    if link:
        add(f"**Dashboard:** {link}")
    add("")

    # ---- summary
    add("## Summary")
    add("")
    add(summary_line(ticket, events))
    if diagnosis:
        d = diagnosis.payload.get("diagnosis") or {}
        add("")
        add(f"Root cause (Investigator, confidence {_conf(d.get('confidence'))}): {d.get('diagnosis', '').strip()}")
    if attempts > 1:
        add("")
        add(f"The agents needed {attempts} attempts.")
    add("")

    # ---- impact
    add("## Impact")
    add("")
    before, at = ticket.metrics_before or {}, ticket.metrics_at_detect or {}
    add("| Metric | Baseline | At detection |")
    add("| --- | --- | --- |")
    add(f"| Error rate | {_pct(before.get('error_rate_pct'))} | {_pct(at.get('error_rate_pct'))} |")
    add(f"| P95 latency | {_ms(before.get('p95_latency_ms'))} | {_ms(at.get('p95_latency_ms'))} |")
    if detected is not None:
        p = detected.payload
        if detected.type is EventType.detected:
            add("")
            add(f"Detected by `{p.get('check')}` at {_ts(detected.ts, tz)}: {p.get('detail', '')}")
            if p.get("breaches"):
                add(f"SLO breaches: {', '.join(p['breaches'])}.")
        else:
            add("")
            add(f"Reported externally at {_ts(detected.ts, tz)}: {p.get('description') or p.get('title') or ''}")
    add("")

    # ---- timeline
    add("## Timeline")
    add("")
    for e in events:
        p = e.payload
        line = None
        if e.type is EventType.detected:
            line = f"incident detected by `{p.get('check')}`"
        elif e.type is EventType.reported:
            line = "incident reported" + (f" from {p['external_ref'].get('system')} {p['external_ref'].get('key')}" if p.get("external_ref") else "")
        elif e.type is EventType.status_change and p.get("to") == TicketStatus.investigating.value and p.get("attempt"):
            line = f"investigation started (attempt {p['attempt']})"
        elif e.type is EventType.diagnosis:
            d = p.get("diagnosis") or {}
            line = f"Investigator posted a root cause (confidence {_conf(d.get('confidence'))})"
        elif e.type is EventType.sandbox_result:
            if p.get("phase") == "before":
                b = p.get("before") or {}
                line = (f"sandbox {p.get('sandbox_run_id')} at `{str(p.get('git_sha', ''))[:10]}`: incident "
                        f"{'reproduced' if p.get('reproduced') else 'NOT reproduced'} (error rate {_pct(b.get('error_rate_pct'))}, "
                        f"p95 {_ms(b.get('p95_ms'))})")
            else:
                a, t = p.get("after") or {}, p.get("tests") or {}
                line = (f"sandbox {p.get('sandbox_run_id')} after the fix: error rate {_pct(a.get('error_rate_pct'))}, "
                        f"p95 {_ms(a.get('p95_ms'))}, tests {'passed' if t.get('passed') else 'FAILED'} — fix "
                        f"{'proven' if p.get('passed') else 'NOT proven'}")
        elif e.type is EventType.finding and p.get("tool") in WRITE_TOOLS and p.get("applied"):
            what = f"{p['tool']} {config_change(p)}" if p.get("tool") == "set_config" else p["tool"]
            line = f"Engineer applied `{what}` → commit `{p.get('commit')}` on `{p.get('branch')}`"
        elif e.type is EventType.review:
            r = p.get("review") or {}
            line = f"Guardian review: **{r.get('decision')}**, risk {r.get('risk_score')}/100"
        elif e.type is EventType.gate_decision:
            checks = p.get("checks") or []
            line = f"command gate: {p.get('status', '').replace('_', ' ')} ({sum(1 for c in checks if c.get('ok'))}/{len(checks)} checks passed)"
        elif e.type is EventType.human_decision:
            line = f"operator {p.get('decision')}d" + (" (override)" if p.get("override") else "") + (f": {p['note']}" if p.get("note") else "")
        elif e.type is EventType.run and p.get("stage") in TIMELINE_STAGES:
            stage = p["stage"]
            if stage == "verify":
                line = f"verify run {p.get('run')}/{p.get('of')} {'passed' if p.get('ok') else 'failed'}"
            elif stage == "monitor":
                line = f"monitor {'ok' if p.get('ok') else 'FAILED'}: {p.get('detail', '')}"
            elif stage == "snapshot":
                line = f"backup taken `{p.get('backup_ref')}`"
            elif stage == "auto_restore":
                line = f"automatic restore: {p.get('reason')}"
            else:
                line = f"deployment stage `{stage}`" + (f" ({p['pct']}%)" if p.get("pct") is not None else "")
        elif e.type is EventType.resolved:
            line = f"**incident resolved** via {p.get('deployment_id')}"
        elif e.type is EventType.rolled_back:
            line = f"**rolled back** {p.get('deployment_id')} to `{p.get('backup_ref')}`: {p.get('reason')}"
        elif e.type is EventType.error:
            line = f"error in {p.get('where')}: {p.get('error')}"
        elif e.type is EventType.status_change and p.get("to") in (TicketStatus.failed.value, TicketStatus.denied.value):
            line = f"ticket {p['to']}" + (f": {p['note']}" if p.get("note") else "")
        if line:
            add(f"- {_ts(e.ts, tz)} {line}")
    add("")

    # ---- root cause
    add("## Root cause")
    add("")
    if diagnosis:
        d = diagnosis.payload.get("diagnosis") or {}
        add(d.get("diagnosis", "").strip() or "No diagnosis text.")
        add("")
        add(f"Confidence: {_conf(d.get('confidence'))}.")
        if d.get("evidence_refs"):
            add("")
            add("Evidence:")
            add("")
            for r in d["evidence_refs"]:
                add(f"- `{r}`")
    else:
        add("The Investigator did not reach a diagnosis.")
    add("")

    # ---- fix
    add("## Fix")
    add("")
    if fixes:
        for e in fixes:
            p = e.payload
            add(f"**{p['tool']}** on `{p.get('branch')}` (commit `{p.get('commit')}`): {', '.join(p.get('files_changed') or [])}")
            add("")
            if p.get("diff"):
                add("```diff")
                add(p["diff"].rstrip())
                add("```")
                add("")
        if deployment is not None:
            add(f"Deployed to production as {deployment.id} (`{deployment.action.value}` {deployment.args}), "
                f"status {deployment.status.value}" + (f", git `{deployment.git_sha}`" if deployment.git_sha else "") + ".")
            add("")
    else:
        add("No fix was applied to the sandbox.")
        add("")

    # ---- validation
    add("## Validation")
    add("")
    if befores or afters:
        b = (befores[-1].payload if befores else {})
        a = (afters[-1].payload if afters else {})
        base = a.get("baseline") or b.get("baseline") or {}
        bm, am, tests = b.get("before") or {}, a.get("after") or {}, a.get("tests") or {}
        add("| | Baseline | Before fix | After fix |")
        add("| --- | --- | --- | --- |")
        add(f"| Error rate | {_pct(base.get('error_rate_pct'))} | {_pct(bm.get('error_rate_pct'))} | {_pct(am.get('error_rate_pct'))} |")
        add(f"| P95 | {_ms(base.get('p95_latency_ms'))} | {_ms(bm.get('p95_ms'))} | {_ms(am.get('p95_ms'))} |")
        tests_cell = ("PASS" if tests.get("passed") else f"FAIL ({tests.get('failed', '?')})") if tests else "—"
        add(f"| Tests | PASS | — | {tests_cell} |")
        add("")
        add(f"Incident reproduced in the sandbox: {'yes' if b.get('reproduced') else 'no'}. "
            f"Fix proven: {'yes' if a.get('passed') else 'no'}. All measurements were taken by OCA, not reported by the agents.")
    else:
        add("No sandbox measurements were recorded.")
    add("")

    # ---- review and approval
    add("## Review and approval")
    add("")
    if review:
        r = review.payload.get("review") or {}
        add(f"Guardian decision: **{r.get('decision')}**, risk score {r.get('risk_score')}/100.")
        if r.get("justification"):
            add("")
            add(f"> {r['justification'].strip()}")
        if r.get("policy_concerns"):
            add("")
            add("Policy concerns: " + "; ".join(r["policy_concerns"]))
        add("")
    if gate:
        checks = gate.payload.get("checks") or []
        add(f"Command gate: {gate.payload.get('status', '').replace('_', ' ')}.")
        add("")
        for c in checks:
            mark = "✓" if c.get("ok") else ("✗" if c.get("ok") is False else "–")
            add(f"- {mark} {c.get('name', '').replace('_', ' ')} — {c.get('detail', '')}")
        add("")
    if human:
        p = human.payload
        add(f"Operator {p.get('decision')}d" + (" (override of a denial)" if p.get("override") else "") + (f": {p['note']}" if p.get("note") else "") + ".")
        add("")
    elif review or gate:
        add("No human intervention was required (human approval switched off).")
        add("")
    if not (review or gate):
        add("The change did not reach review.")
        add("")

    # ---- follow-ups
    follow: list[str] = []
    if diagnosis:
        follow += [f"Open question from the Investigator: {q}" for q in (diagnosis.payload.get("diagnosis") or {}).get("unresolved_questions") or []]
    if review:
        follow += [f"Guardian concern: {c}" for c in (review.payload.get("review") or {}).get("policy_concerns") or []]
    if rolled_back:
        follow.append(f"The deployment was rolled back ({rolled_back.payload.get('reason')}); the fix needs rework.")
    if ticket.status is TicketStatus.failed:
        follow.append("OCA gave up; a human must take over this incident.")
    if errors:
        follow += [f"Error during the run ({e.payload.get('where')}): {e.payload.get('error')}" for e in errors[-5:]]
    add("## Follow-ups")
    add("")
    if follow:
        for f in follow:
            add(f"- {f}")
    else:
        add("None recorded.")
    add("")
    add("---")
    add(f"_Generated by OCA at {_dt(datetime.now(timezone.utc), tz)} from {len(events)} ticket events. "
        "Agent text is quoted as written; every measurement was taken by OCA on this machine._")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------- service


class ReportWriter:
    """Writes the report when a ticket ends. Debounced per ticket so the trailing `resolved` event is included."""

    def __init__(self, app: FastAPI, delay_s: float = REPORT_DELAY_S) -> None:
        self.app = app
        self.db = app.state.db
        self.settings = app.state.settings
        self.directory: Path = Path(self.settings.data_dir) / "reports"
        self.delay_s = delay_s
        self.queue: asyncio.Queue = app.state.bus.subscribe()
        self._pending: dict[str, asyncio.Task] = {}

    # -- building

    async def render(self, ticket_id: str) -> str | None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            return None
        events = await self.db.list_events(ticket_id)
        mttr = await self.db.mttr(ticket_id) if ticket.status is TicketStatus.resolved else None
        deployment = await self.db.get_deployment(ticket.current_deployment_id) if ticket.current_deployment_id else None
        return build_report(ticket, events, mttr, deployment, tz=getattr(self.app.state, "tz", None),
                            public_url=self.settings.public_url)

    def path_for(self, ticket_id: str) -> Path:
        return self.directory / f"{ticket_id}.md"

    async def write(self, ticket_id: str) -> TicketEvent | None:
        ticket = await self.db.get_ticket(ticket_id)
        text = await self.render(ticket_id)
        if ticket is None or text is None:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ticket_id)
        path.write_text(text, encoding="utf-8")
        events = await self.db.list_events(ticket_id)
        url = f"{self.settings.public_url.rstrip('/')}/tickets/{ticket_id}/report"
        event = await self.db.append_event(ticket_id, TicketEventCreate(actor=Actor.system, type=EventType.report, payload={
            "status": ticket.status.value, "path": str(path), "url": url, "chars": len(text),
            "summary": summary_line(ticket, events)}))
        log.info("incident report written for %s (%s, %d chars)", ticket_id, ticket.status.value, len(text))
        return event

    # -- bus handling

    def handle(self, event: TicketEvent) -> None:
        terminal = (event.type is EventType.status_change and event.payload.get("to") in {s.value for s in TERMINAL_STATUSES}) \
            or event.type in (EventType.resolved, EventType.rolled_back)
        if not terminal:
            return
        previous = self._pending.pop(event.ticket_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._pending[event.ticket_id] = asyncio.create_task(self._write_later(event.ticket_id), name=f"oca-report-{event.ticket_id}")

    async def _write_later(self, ticket_id: str) -> None:
        try:
            await asyncio.sleep(self.delay_s)
            await self.write(ticket_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a report must never break the pipeline
            log.exception("incident report for %s failed", ticket_id)
        finally:
            if self._pending.get(ticket_id) is asyncio.current_task():
                self._pending.pop(ticket_id, None)

    async def run_forever(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                self.handle(event)
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        """Wait until every published event has been seen and every scheduled report is written (tests)."""
        await self.queue.join()
        while self._pending:
            await asyncio.gather(*list(self._pending.values()), return_exceptions=True)


async def start_reports(app: FastAPI) -> asyncio.Task:
    app.state.reports = ReportWriter(app)
    return asyncio.create_task(app.state.reports.run_forever(), name="oca-reports")
