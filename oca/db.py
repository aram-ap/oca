"""SQLite data layer. One aiosqlite connection, one write lock, typed results."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from oca.events import EventBus
from oca.ids import format_id
from oca.schemas import (
    Action, ActionRequest, ActionName, ActionStatus, Actor, CheckResult, Deployment,
    DeploymentStatus, EventType, GateDecision, HealthCheckInfo, MTTR, OPEN_STATUSES,
    OutboundMessage, OutboxStatus, SandboxRun, Ticket, TicketCreate, TicketEvent,
    TicketEventCreate, TicketStatus, TicketWithEvents, utcnow,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _load(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default


class Database:
    def __init__(self, path: str | Path, bus: EventBus | None = None) -> None:
        self.path = Path(path)
        self.bus = bus or EventBus()
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> "Database":
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def next_id(self, prefix: str) -> str:
        """Allocate the next INC-/DEP-/SBX- identifier. Call while holding the write lock."""
        row = await self._one(
            "INSERT INTO counters(name, next) VALUES(?, 1) "
            "ON CONFLICT(name) DO UPDATE SET next = next + 1 RETURNING next", (prefix,))
        assert row is not None
        return format_id(prefix, row[0])

    # -------------------------------------------------------------------- tickets

    @staticmethod
    def _ticket(row: aiosqlite.Row) -> Ticket:
        return Ticket(
            id=row["id"], source=row["source"], service=row["service"], title=row["title"],
            status=row["status"], severity=row["severity"], description=row["description"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
            resolved_at=_dt(row["resolved_at"]),
            metrics_before=_load(row["metrics_before"]), metrics_at_detect=_load(row["metrics_at_detect"]),
            external_ref=_load(row["external_ref"]), attempt=row["attempt"],
            current_deployment_id=row["current_deployment_id"], pending_action_id=row["pending_action_id"],
        )

    async def create_ticket(self, data: TicketCreate, first_event: TicketEventCreate | None = None,
                            status: TicketStatus = TicketStatus.detected) -> Ticket:
        async with self._lock:
            ticket_id = await self.next_id("INC")
            now = _iso(utcnow())
            await self.conn.execute(
                "INSERT INTO tickets(id, source, service, title, status, severity, description, created_at, "
                "updated_at, metrics_before, metrics_at_detect, external_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, data.source.value, data.service, data.title, status.value, data.severity.value,
                 data.description, now, now, _dump(data.metrics_before) if data.metrics_before is not None else None,
                 _dump(data.metrics_at_detect) if data.metrics_at_detect is not None else None,
                 _dump(data.external_ref) if data.external_ref is not None else None))
        if first_event is not None:
            await self.append_event(ticket_id, first_event)
        ticket = await self.get_ticket(ticket_id)
        assert ticket is not None
        return ticket

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        row = await self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return self._ticket(row) if row else None

    async def get_ticket_with_events(self, ticket_id: str) -> TicketWithEvents | None:
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            return None
        events = await self.list_events(ticket_id)
        return TicketWithEvents(**ticket.model_dump(), events=events)

    async def list_tickets(self, status: TicketStatus | None = None, service: str | None = None,
                           open_only: bool = False, limit: int = 100) -> list[Ticket]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if open_only:
            marks = ",".join("?" * len(OPEN_STATUSES))
            clauses.append(f"status IN ({marks})")
            params.extend(s.value for s in OPEN_STATUSES)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._all(f"SELECT * FROM tickets {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))
        return [self._ticket(r) for r in rows]

    async def open_ticket_for_service(self, service: str) -> Ticket | None:
        tickets = await self.list_tickets(service=service, open_only=True, limit=1)
        return tickets[0] if tickets else None

    async def blocking_ticket_for_service(self, service: str) -> Ticket | None:
        """Newest ticket that should stop the scheduler opening another: anything not resolved.

        failed / rolled_back / denied tickets need a human; opening a new ticket every
        interval while the incident persists would only bury the evidence.
        """
        row = await self._one("SELECT * FROM tickets WHERE service = ? AND status != ? ORDER BY created_at DESC LIMIT 1",
                              (service, TicketStatus.resolved.value))
        return self._ticket(row) if row else None

    async def last_resolved_at(self, service: str) -> datetime | None:
        row = await self._one("SELECT MAX(resolved_at) AS t FROM tickets WHERE service = ? AND resolved_at IS NOT NULL",
                              (service,))
        return _dt(row["t"]) if row and row["t"] else None

    async def find_ticket_by_external_ref(self, system: str, key: str) -> Ticket | None:
        """The ticket created from an external system's ticket (e.g. Jira KAN-7), if any."""
        row = await self._one(
            "SELECT * FROM tickets WHERE json_extract(external_ref, '$.system') = ? "
            "AND json_extract(external_ref, '$.key') = ? ORDER BY created_at DESC LIMIT 1", (system, key))
        return self._ticket(row) if row else None

    _TICKET_COLUMNS = frozenset({"title", "severity", "description", "resolved_at", "metrics_before",
                                 "metrics_at_detect", "external_ref", "attempt", "current_deployment_id",
                                 "pending_action_id", "status"})

    async def update_ticket(self, ticket_id: str, **fields: Any) -> Ticket:
        unknown = set(fields) - self._TICKET_COLUMNS
        if unknown:
            raise ValueError(f"Unknown ticket fields: {sorted(unknown)}")
        sets, params = ["updated_at = ?"], [_iso(utcnow())]
        for key, value in fields.items():
            if isinstance(value, dict):
                value = _dump(value)
            elif isinstance(value, datetime):
                value = _iso(value)
            elif hasattr(value, "value"):
                value = value.value
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(ticket_id)
        async with self._lock:
            await self.conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params)
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        return ticket

    async def set_status(self, ticket_id: str, status: TicketStatus, actor: Actor,
                         note: str | None = None, **extra: Any) -> Ticket:
        current = await self.get_ticket(ticket_id)
        if current is None:
            raise KeyError(ticket_id)
        fields: dict[str, Any] = {"status": status}
        if status == TicketStatus.resolved:
            fields["resolved_at"] = utcnow()
        ticket = await self.update_ticket(ticket_id, **fields)
        payload = {"from": current.status.value, "to": status.value, **extra}
        if note:
            payload["note"] = note
        await self.append_event(ticket_id, TicketEventCreate(actor=actor, type=EventType.status_change, payload=payload))
        return ticket

    # --------------------------------------------------------------------- events

    @staticmethod
    def _event(row: aiosqlite.Row) -> TicketEvent:
        return TicketEvent(id=row["id"], ticket_id=row["ticket_id"], ts=_dt(row["ts"]),
                           actor=row["actor"], type=row["type"], payload=_load(row["payload"], {}))

    async def append_event(self, ticket_id: str, event: TicketEventCreate) -> TicketEvent:
        async with self._lock:
            row = await self._one(
                "INSERT INTO ticket_events(ticket_id, ts, actor, type, payload) VALUES(?,?,?,?,?) RETURNING *",
                (ticket_id, _iso(utcnow()), event.actor.value, event.type.value, _dump(event.payload)))
            await self.conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (_iso(utcnow()), ticket_id))
        assert row is not None
        stored = self._event(row)
        self.bus.publish(stored)
        return stored

    async def list_events(self, ticket_id: str, after_id: int = 0, limit: int = 2000) -> list[TicketEvent]:
        rows = await self._all("SELECT * FROM ticket_events WHERE ticket_id = ? AND id > ? ORDER BY id LIMIT ?",
                               (ticket_id, after_id, limit))
        return [self._event(r) for r in rows]

    async def recent_events(self, limit: int = 200, after_id: int = 0) -> list[TicketEvent]:
        rows = await self._all("SELECT * FROM ticket_events WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit))
        return [self._event(r) for r in reversed(rows)]

    async def latest_event(self, ticket_id: str, type: EventType) -> TicketEvent | None:
        row = await self._one("SELECT * FROM ticket_events WHERE ticket_id = ? AND type = ? ORDER BY id DESC LIMIT 1",
                              (ticket_id, type.value))
        return self._event(row) if row else None

    # -------------------------------------------------------------------- actions

    @staticmethod
    def _action(row: aiosqlite.Row) -> Action:
        return Action(id=row["id"], ticket_id=row["ticket_id"], action=row["action"], args=_load(row["args"], {}),
                      requested_by=row["requested_by"], evidence=_load(row["evidence"], {}), status=row["status"],
                      checks=_load(row["checks"], []), reason=row["reason"], created_at=_dt(row["created_at"]),
                      decided_at=_dt(row["decided_at"]), decided_by=row["decided_by"],
                      deployment_id=row["deployment_id"])

    async def create_action(self, request: ActionRequest, decision: GateDecision) -> Action:
        async with self._lock:
            row = await self._one(
                "INSERT INTO actions(ticket_id, action, args, requested_by, evidence, status, checks, reason, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) RETURNING *",
                (request.ticket_id, request.action.value, _dump(request.args), request.requested_by.value,
                 _dump(request.evidence.model_dump(mode="json")), decision.status.value,
                 _dump([c.model_dump() for c in decision.checks]), decision.reason, _iso(utcnow())))
        assert row is not None
        return self._action(row)

    async def get_action(self, action_id: int) -> Action | None:
        row = await self._one("SELECT * FROM actions WHERE id = ?", (action_id,))
        return self._action(row) if row else None

    async def latest_action(self, ticket_id: str) -> Action | None:
        row = await self._one("SELECT * FROM actions WHERE ticket_id = ? ORDER BY id DESC LIMIT 1", (ticket_id,))
        return self._action(row) if row else None

    async def update_action(self, action_id: int, status: ActionStatus, decided_by: str | None = None,
                            deployment_id: str | None = None, reason: str | None = None) -> Action:
        async with self._lock:
            await self.conn.execute(
                "UPDATE actions SET status = ?, decided_at = COALESCE(?, decided_at), decided_by = COALESCE(?, decided_by), "
                "deployment_id = COALESCE(?, deployment_id), reason = COALESCE(?, reason) WHERE id = ?",
                (status.value, _iso(utcnow()) if decided_by else None, decided_by, deployment_id, reason, action_id))
        action = await self.get_action(action_id)
        if action is None:
            raise KeyError(action_id)
        return action

    # ---------------------------------------------------------------- deployments

    @staticmethod
    def _deployment(row: aiosqlite.Row) -> Deployment:
        return Deployment(id=row["id"], ticket_id=row["ticket_id"], action=row["action"], args=_load(row["args"], {}),
                          backup_ref=row["backup_ref"], status=row["status"], started_at=_dt(row["started_at"]),
                          finished_at=_dt(row["finished_at"]), evidence=_load(row["evidence"], {}),
                          git_sha=row["git_sha"], prev_sha=row["prev_sha"],
                          driftwood_deploy_id=row["driftwood_deploy_id"], note=row["note"])

    async def create_deployment(self, ticket_id: str | None, action: ActionName, args: dict[str, Any],
                                evidence: dict[str, Any] | None = None, note: str | None = None,
                                status: DeploymentStatus = DeploymentStatus.pending) -> Deployment:
        async with self._lock:
            dep_id = await self.next_id("DEP")
            await self.conn.execute(
                "INSERT INTO deployments(id, ticket_id, action, args, status, started_at, evidence, note) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (dep_id, ticket_id, action.value, _dump(args), status.value, _iso(utcnow()), _dump(evidence or {}), note))
        deployment = await self.get_deployment(dep_id)
        assert deployment is not None
        return deployment

    _DEPLOYMENT_COLUMNS = frozenset({"backup_ref", "status", "finished_at", "evidence", "git_sha", "prev_sha",
                                     "driftwood_deploy_id", "note"})

    async def update_deployment(self, dep_id: str, **fields: Any) -> Deployment:
        unknown = set(fields) - self._DEPLOYMENT_COLUMNS
        if unknown:
            raise ValueError(f"Unknown deployment fields: {sorted(unknown)}")
        sets, params = [], []
        for key, value in fields.items():
            if isinstance(value, dict):
                value = _dump(value)
            elif isinstance(value, datetime):
                value = _iso(value)
            elif hasattr(value, "value"):
                value = value.value
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            raise ValueError("No deployment fields to update")
        params.append(dep_id)
        async with self._lock:
            await self.conn.execute(f"UPDATE deployments SET {', '.join(sets)} WHERE id = ?", params)
        deployment = await self.get_deployment(dep_id)
        if deployment is None:
            raise KeyError(dep_id)
        return deployment

    async def get_deployment(self, dep_id: str) -> Deployment | None:
        row = await self._one("SELECT * FROM deployments WHERE id = ?", (dep_id,))
        return self._deployment(row) if row else None

    async def list_deployments(self, limit: int = 100) -> list[Deployment]:
        rows = await self._all("SELECT * FROM deployments ORDER BY started_at DESC LIMIT ?", (limit,))
        return [self._deployment(r) for r in rows]

    async def last_deployment_for_service(self, service: str, since: datetime | None = None) -> Deployment | None:
        row = await self._one(
            "SELECT d.* FROM deployments d LEFT JOIN tickets t ON t.id = d.ticket_id "
            "WHERE (t.service = ? OR json_extract(d.args, '$.service') = ?) AND d.started_at >= ? "
            "ORDER BY d.started_at DESC LIMIT 1",
            (service, service, _iso(since) if since else "0000"))
        return self._deployment(row) if row else None

    # --------------------------------------------------------------- sandbox runs

    @staticmethod
    def _sandbox_run(row: aiosqlite.Row) -> SandboxRun:
        return SandboxRun(id=row["id"], ticket_id=row["ticket_id"], patch_ref=row["patch_ref"], base_sha=row["base_sha"],
                          before=_load(row["before_json"]), after=_load(row["after_json"]), tests=_load(row["tests_json"]),
                          baseline=_load(row["baseline"], {}),
                          reproduced=None if row["reproduced"] is None else bool(row["reproduced"]),
                          passed=None if row["passed"] is None else bool(row["passed"]),
                          created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]), notes=row["notes"])

    async def create_sandbox_run(self, ticket_id: str, base_sha: str | None, baseline: dict[str, float]) -> SandboxRun:
        async with self._lock:
            run_id = await self.next_id("SBX")
            now = _iso(utcnow())
            await self.conn.execute(
                "INSERT INTO sandbox_runs(id, ticket_id, base_sha, baseline, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (run_id, ticket_id, base_sha, _dump(baseline), now, now))
        run = await self.get_sandbox_run(run_id)
        assert run is not None
        return run

    _SANDBOX_COLUMNS = {"patch_ref": "patch_ref", "base_sha": "base_sha", "before": "before_json",
                        "after": "after_json", "tests": "tests_json", "baseline": "baseline",
                        "reproduced": "reproduced", "passed": "passed", "notes": "notes"}

    async def update_sandbox_run(self, run_id: str, **fields: Any) -> SandboxRun:
        unknown = set(fields) - set(self._SANDBOX_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown sandbox run fields: {sorted(unknown)}")
        sets, params = ["updated_at = ?"], [_iso(utcnow())]
        for key, value in fields.items():
            if hasattr(value, "model_dump"):
                value = _dump(value.model_dump(mode="json"))
            elif isinstance(value, dict):
                value = _dump(value)
            elif isinstance(value, bool):
                value = int(value)
            sets.append(f"{self._SANDBOX_COLUMNS[key]} = ?")
            params.append(value)
        params.append(run_id)
        async with self._lock:
            await self.conn.execute(f"UPDATE sandbox_runs SET {', '.join(sets)} WHERE id = ?", params)
        run = await self.get_sandbox_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    async def get_sandbox_run(self, run_id: str) -> SandboxRun | None:
        row = await self._one("SELECT * FROM sandbox_runs WHERE id = ?", (run_id,))
        return self._sandbox_run(row) if row else None

    async def latest_sandbox_run(self, ticket_id: str) -> SandboxRun | None:
        row = await self._one("SELECT * FROM sandbox_runs WHERE ticket_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                              (ticket_id,))
        return self._sandbox_run(row) if row else None

    # -------------------------------------------------------------- health checks

    @staticmethod
    def _check(row: aiosqlite.Row) -> HealthCheckInfo:
        raw = _load(row["last_result"])
        return HealthCheckInfo(name=row["name"], service=row["service"], enabled=bool(row["enabled"]),
                               interval_s=row["interval_s"], opens_ticket=bool(row["opens_ticket"]),
                               last_run=_dt(row["last_run"]),
                               last_ok=None if row["last_ok"] is None else bool(row["last_ok"]),
                               last_result=CheckResult(**raw) if raw else None)

    async def register_check(self, name: str, service: str, interval_s: int, enabled: bool = True,
                             opens_ticket: bool = True) -> HealthCheckInfo:
        """Insert if missing. Operator changes to enabled/interval survive restarts."""
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO health_checks(name, service, enabled, interval_s, opens_ticket) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET service = excluded.service, opens_ticket = excluded.opens_ticket",
                (name, service, int(enabled), interval_s, int(opens_ticket)))
        info = await self.get_check(name)
        assert info is not None
        return info

    async def record_check_result(self, name: str, result: CheckResult) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE health_checks SET last_run = ?, last_ok = ?, last_result = ? WHERE name = ?",
                (_iso(result.ts), int(result.ok), _dump(result.model_dump(mode="json")), name))

    async def update_check(self, name: str, enabled: bool | None = None, interval_s: int | None = None) -> HealthCheckInfo:
        async with self._lock:
            await self.conn.execute(
                "UPDATE health_checks SET enabled = COALESCE(?, enabled), interval_s = COALESCE(?, interval_s) WHERE name = ?",
                (None if enabled is None else int(enabled), interval_s, name))
        info = await self.get_check(name)
        if info is None:
            raise KeyError(name)
        return info

    async def get_check(self, name: str) -> HealthCheckInfo | None:
        row = await self._one("SELECT * FROM health_checks WHERE name = ?", (name,))
        return self._check(row) if row else None

    async def list_checks(self) -> list[HealthCheckInfo]:
        return [self._check(r) for r in await self._all("SELECT * FROM health_checks ORDER BY name")]

    # ------------------------------------------------------------------- settings

    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return _load(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _dump(value)))

    async def seed_setting(self, key: str, value: Any) -> None:
        """Set only if absent, so operator edits win over environment defaults."""
        async with self._lock:
            await self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, _dump(value)))

    async def all_settings(self) -> dict[str, Any]:
        return {r["key"]: _load(r["value"]) for r in await self._all("SELECT key, value FROM settings ORDER BY key")}

    # --------------------------------------------------------------------- outbox

    async def enqueue_outbox(self, message: OutboundMessage) -> int:
        async with self._lock:
            row = await self._one(
                "INSERT INTO integration_outbox(ticket_id, target, kind, payload, next_attempt) VALUES(?,?,?,?,?) RETURNING id",
                (message.ticket_id, message.target, message.kind, _dump(message.payload), _iso(utcnow())))
        assert row is not None
        return row[0]

    async def due_outbox(self, limit: int = 20) -> list[dict[str, Any]]:
        """Pending messages whose retry time has come, oldest first.

        Messages for one ticket and target go out in order: a message waits while an
        earlier one for the same ticket and target is still pending (for example a
        Slack update waits for the message that starts its thread).
        """
        rows = await self._all(
            "SELECT o.* FROM integration_outbox o WHERE o.status = 'pending' AND o.next_attempt <= ? "
            "AND NOT EXISTS (SELECT 1 FROM integration_outbox p WHERE p.ticket_id = o.ticket_id "
            "AND p.target = o.target AND p.status = 'pending' AND p.id < o.id) "
            "ORDER BY o.id LIMIT ?",
            (_iso(utcnow()), limit))
        return [{**dict(r), "payload": _load(r["payload"], {})} for r in rows]

    async def mark_outbox(self, item_id: int, status: OutboxStatus, attempts: int, bytes_sent: int = 0,
                          retry_in_s: float | None = None, error: str | None = None) -> None:
        next_attempt = utcnow() + timedelta(seconds=retry_in_s or 0)
        async with self._lock:
            await self.conn.execute(
                "UPDATE integration_outbox SET status = ?, attempts = ?, bytes_sent = bytes_sent + ?, next_attempt = ?, "
                "last_error = ? WHERE id = ?",
                (status.value, attempts, bytes_sent, _iso(next_attempt), error, item_id))

    async def outbox_stats(self) -> dict[str, dict[str, int]]:
        rows = await self._all(
            "SELECT target, SUM(status = 'pending') AS pending, SUM(status = 'failed') AS failed, "
            "SUM(bytes_sent) AS bytes_sent FROM integration_outbox GROUP BY target")
        return {r["target"]: {"pending": r["pending"] or 0, "failed": r["failed"] or 0,
                              "bytes_sent": r["bytes_sent"] or 0} for r in rows}

    # ---------------------------------------------------------- integration links

    async def get_link(self, ticket_id: str, target: str) -> dict[str, Any] | None:
        row = await self._one("SELECT ref FROM integration_links WHERE ticket_id = ? AND target = ?",
                              (ticket_id, target))
        return _load(row["ref"], {}) if row else None

    async def set_link(self, ticket_id: str, target: str, ref: dict[str, Any]) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO integration_links(ticket_id, target, ref, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ticket_id, target) DO UPDATE SET ref = excluded.ref, updated_at = excluded.updated_at",
                (ticket_id, target, _dump(ref), _iso(utcnow())))

    async def total_bytes_sent(self) -> int:
        row = await self._one("SELECT COALESCE(SUM(bytes_sent), 0) AS b FROM integration_outbox")
        return int(row["b"]) if row else 0

    # ---------------------------------------------------------------------- stats

    async def mttr(self, ticket_id: str) -> MTTR:
        events = await self.list_events(ticket_id)

        def first(*types: EventType, where=None) -> datetime | None:
            for e in events:
                if e.type in types and (where is None or where(e)):
                    return e.ts
            return None

        def last(*types: EventType, where=None) -> datetime | None:
            found = None
            for e in events:
                if e.type in types and (where is None or where(e)):
                    found = e.ts
            return found

        detected = first(EventType.detected, EventType.reported)
        diagnosis = first(EventType.diagnosis)
        patch = first(EventType.patch)
        validated = first(EventType.sandbox_result, where=lambda e: e.payload.get("passed") is True)
        approved = last(EventType.status_change, where=lambda e: e.payload.get("to") == TicketStatus.approved.value)
        resolved = first(EventType.resolved)
        if resolved is None:
            ticket = await self.get_ticket(ticket_id)
            resolved = ticket.resolved_at if ticket else None

        def delta(a: datetime | None, b: datetime | None) -> float | None:
            return round((b - a).total_seconds(), 1) if a and b else None

        detection = None
        for e in events:
            if e.type == EventType.detected and isinstance(e.payload.get("detected_after_s"), (int, float)):
                detection = float(e.payload["detected_after_s"])
        return MTTR(detection_s=detection, diagnosis_s=delta(detected, diagnosis), fix_s=delta(diagnosis, patch),
                    validation_s=delta(patch, validated), deployment_s=delta(approved, resolved),
                    total_s=delta(detected, resolved))
