"""Primary detector: driftwood's own ``ops/status.py --json`` (SLO: 5% errors, 1 s p95, 90% pool)."""

from __future__ import annotations

from oca.checks.base import HealthCheck
from oca.schemas import CheckResult

LABELS = {"error_rate_pct": "error rate {v:.1f}% (>5%)", "p95_latency_ms": "p95 {v:.0f} ms (>1000 ms)",
          "db_pool_utilization_pct": "DB pool {v:.0f}% (>90%)"}


class StatusCheck(HealthCheck):
    name = "status_check"
    service = "orders-api"

    def __init__(self, runner, interval_s: int = 10) -> None:
        self.runner, self.interval_s = runner, interval_s

    async def run(self) -> CheckResult:
        snap = await self.runner.status()
        metrics = snap.metrics()
        if snap.healthy:
            detail = f"healthy: error rate {snap.error_rate_pct:.1f}%, p95 {snap.p95_latency_ms:.0f} ms, {snap.rps:.0f} rps"
        else:
            parts = [LABELS.get(b, b).format(v=metrics.get(b, 0.0)) for b in snap.breaches]
            detail = "INCIDENT: " + ", ".join(parts)
            if snap.db_errors:
                worst = max(snap.db_errors.items(), key=lambda kv: kv[1])
                detail += f"; top DB error {worst[0]} at {worst[1]:.1f}/s"
        return CheckResult(ok=snap.healthy, check=self.name, service=self.service, detail=detail,
                           metrics=metrics, breaches=list(snap.breaches), raw=snap.as_dict())
