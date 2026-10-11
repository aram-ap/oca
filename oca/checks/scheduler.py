"""Runs checks on their intervals, opens one ticket per failing service, learns the baseline."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from oca.checks import PRIMARY_CHECK, HealthCheck, build_checks
from oca.schemas import (
    Actor, CheckResult, EventType, Severity, TicketCreate, TicketEventCreate, TicketSource,
)

log = logging.getLogger("oca.scheduler")
CHECK_TIMEOUT_S = 60
CHECK_FAILED_EVENT_INTERVAL_S = 60
BASELINE_STREAK = 5
CONFIRM_BREACHES = frozenset({"endpoint"})  # a refused connection during a redeploy is normal: confirm on the next run
CONFIRM_RUNS = 2


def severity_for(result: CheckResult) -> Severity:
    error = result.metrics.get("error_rate_pct", 0.0)
    if error > 20 or "db_pool_utilization_pct" in result.breaches:
        return Severity.critical
    if "error_rate_pct" in result.breaches or "endpoint" in result.breaches:
        return Severity.high
    return Severity.medium


def title_for(result: CheckResult) -> str:
    m = result.metrics
    if "error_rate_pct" in m and "p95_latency_ms" in m:
        return f"{result.service}: error rate {m['error_rate_pct']:.1f}% / p95 {m['p95_latency_ms']:.0f} ms"
    return f"{result.service}: {result.detail[:80]}"


class Scheduler:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.db = app.state.db
        self.settings = app.state.settings
        self.checks: dict[str, HealthCheck] = {c.name: c for c in build_checks(
            self.settings, app.state.runner, self.db, getattr(app.state, "integrations", None))}
        self._last_failed_event: dict[str, float] = {}
        self._ok_streak: dict[str, int] = {}
        self._fail_streak: dict[str, int] = {}
        self._lock = asyncio.Lock()  # ticks and run-now calls never overlap, so a service opens one ticket at a time
        self.ticks = 0

    async def register(self) -> None:
        for check in self.checks.values():
            await self.db.register_check(check.name, check.service, check.interval_s,
                                         enabled=check.enabled_by_default, opens_ticket=check.opens_ticket)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the scheduler must survive a bad check
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.settings.scheduler_tick_s)

    async def tick(self, force: bool = False) -> list[CheckResult]:
        async with self._lock:
            self.ticks += 1
            now = datetime.now(timezone.utc)
            results = []
            for info in await self.db.list_checks():
                check = self.checks.get(info.name)
                if check is None or not info.enabled:
                    continue
                due = force or info.last_run is None or now - info.last_run >= timedelta(seconds=info.interval_s)
                if due:
                    results.append(await self._run_check(info.name, True))
            return results

    async def run_check(self, name: str, handle: bool = True) -> CheckResult:
        async with self._lock:
            return await self._run_check(name, handle)

    async def _run_check(self, name: str, handle: bool) -> CheckResult:
        check = self.checks[name]
        try:
            result = await asyncio.wait_for(check.run(), CHECK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a failed check
            result = CheckResult(ok=False, check=name, service=check.service,
                                 detail=f"check error: {type(exc).__name__}: {exc}", breaches=["check_error"])
        await self.db.record_check_result(name, result)
        if handle:
            await self._handle(check, result)
        return result

    async def _handle(self, check: HealthCheck, result: CheckResult) -> None:
        if result.ok:
            self._fail_streak[check.name] = 0
            streak = self._ok_streak.get(check.name, 0) + 1
            self._ok_streak[check.name] = streak
            if check.name == PRIMARY_CHECK and streak >= BASELINE_STREAK and streak % BASELINE_STREAK == 0:
                await self._learn_baseline(result)
            return
        self._ok_streak[check.name] = 0
        fails = self._fail_streak[check.name] = self._fail_streak.get(check.name, 0) + 1
        if not check.opens_ticket or result.breaches == ["check_error"]:
            return
        existing = await self.db.blocking_ticket_for_service(check.service)
        if existing is not None:
            last = self._last_failed_event.get(existing.id, 0.0)
            if time.monotonic() - last >= CHECK_FAILED_EVENT_INTERVAL_S:
                self._last_failed_event[existing.id] = time.monotonic()
                await self.db.append_event(existing.id, TicketEventCreate(
                    actor=Actor.health_check, type=EventType.check_failed,
                    payload={"check": check.name, "detail": result.detail, "metrics": result.metrics}))
            return
        if set(result.breaches) <= CONFIRM_BREACHES and fails < CONFIRM_RUNS:
            log.info("%s failed once (%s); waiting for the next run before opening a ticket", check.name, result.detail)
            return
        cooldown = int(await self.db.get_setting("reopen_cooldown_s", 120) or 0)
        last_resolved = await self.db.last_resolved_at(check.service)
        if last_resolved and datetime.now(timezone.utc) - last_resolved < timedelta(seconds=cooldown):
            log.info("%s failing again within reopen cooldown; not opening a ticket", check.service)
            return
        baseline = await self.db.get_setting("baseline", {}) or {}
        ticket = await self.db.create_ticket(
            TicketCreate(source=TicketSource.health_check, service=check.service, title=title_for(result),
                         severity=severity_for(result), metrics_before=baseline, metrics_at_detect=result.metrics),
            TicketEventCreate(actor=Actor.health_check, type=EventType.detected,
                              payload={"check": check.name, "detail": result.detail, "metrics": result.metrics,
                                       "breaches": result.breaches, "status": result.raw}))
        self._last_failed_event[ticket.id] = time.monotonic()
        log.warning("opened %s for %s: %s", ticket.id, check.service, result.detail)
        pipeline = getattr(self.app.state, "pipeline", None)
        if pipeline is not None and self.settings.autostart_pipeline:
            pipeline.launch(ticket.id)

    async def _learn_baseline(self, result: CheckResult) -> None:
        baseline = dict(await self.db.get_setting("baseline", {}) or {})
        changed = False
        for key in ("error_rate_pct", "p95_latency_ms"):
            if key in result.metrics:
                old = float(baseline.get(key, result.metrics[key]))
                baseline[key] = round(0.8 * old + 0.2 * float(result.metrics[key]), 3)
                changed = True
        if changed:
            await self.db.set_setting("baseline", baseline)


async def start_scheduler(app: FastAPI) -> asyncio.Task:
    scheduler = Scheduler(app)
    await scheduler.register()
    app.state.scheduler = scheduler
    return asyncio.create_task(scheduler.run_forever(), name="oca-scheduler")
