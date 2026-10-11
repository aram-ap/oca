"""Share of 5xx lines in the last minute of orders-api logs. Evidence only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oca.checks.base import HealthCheck
from oca.schemas import CheckResult


def _ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class LogErrorParse(HealthCheck):
    name = "log_error_parse"
    service = "orders-api"
    opens_ticket = False

    def __init__(self, runner, interval_s: int = 15, window_s: int = 60, threshold_pct: float = 5.0) -> None:
        self.runner, self.interval_s, self.window_s, self.threshold_pct = runner, interval_s, window_s, threshold_pct

    async def run(self) -> CheckResult:
        rows = await self.runner.read_logs(self.service, lines=1000)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.window_s)
        total = errors = 0
        messages: dict[str, int] = {}
        for row in rows:
            ts = _ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            status = row.get("status")
            if isinstance(status, int):
                total += 1
                if status >= 500:
                    errors += 1
                    msg = str(row.get("msg", "error"))
                    messages[msg] = messages.get(msg, 0) + 1
        rate = 100.0 * errors / total if total else 0.0
        top = max(messages.items(), key=lambda kv: kv[1])[0] if messages else None
        detail = f"{errors}/{total} requests 5xx in last {self.window_s}s ({rate:.1f}%)" + (f"; top: {top!r}" if top else "")
        return CheckResult(ok=rate <= self.threshold_pct, check=self.name, service=self.service, detail=detail,
                           metrics={"error_rate_pct": round(rate, 2), "requests": float(total)},
                           breaches=[] if rate <= self.threshold_pct else ["error_rate_pct"],
                           raw={"top_messages": messages})
