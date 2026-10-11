"""Recorded Prometheus series vs the learned baseline. Evidence only; does not open tickets."""

from __future__ import annotations

from oca.checks.base import HealthCheck
from oca.schemas import CheckResult


class MetricsCheck(HealthCheck):
    name = "metrics_check"
    service = "orders-api"
    opens_ticket = False

    def __init__(self, runner, db, interval_s: int = 15) -> None:
        self.runner, self.db, self.interval_s = runner, db, interval_s

    async def run(self) -> CheckResult:
        series = await self.runner.metrics(self.service)
        baseline = await self.db.get_setting("baseline", {}) or {}
        thresholds = await self.db.get_setting("thresholds", {}) or {}
        factor = float(thresholds.get("metrics_factor", 5.0))
        error_pct = 100 * series.get("orders_api:error_rate:1m", 0.0)
        p95_ms = 1000 * series.get("orders_api:p95_latency_seconds:1m", 0.0)
        breaches = []
        if error_pct > max(float(baseline.get("error_rate_pct", 0.0)) * factor, float(thresholds.get("error_rate_pct", 5.0))):
            breaches.append("error_rate_pct")
        if p95_ms > max(float(baseline.get("p95_latency_ms", 20.0)) * factor, float(thresholds.get("p95_latency_ms", 1000.0))):
            breaches.append("p95_latency_ms")
        detail = (f"error rate {error_pct:.2f}% vs baseline {baseline.get('error_rate_pct', 0.0)}%; "
                  f"p95 {p95_ms:.0f} ms vs baseline {baseline.get('p95_latency_ms', 20.0)} ms")
        return CheckResult(ok=not breaches, check=self.name, service=self.service, detail=detail,
                           metrics={"error_rate_pct": round(error_pct, 3), "p95_latency_ms": round(p95_ms, 1)},
                           breaches=breaches, raw={"series": series})
