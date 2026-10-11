from __future__ import annotations

from oca.checks.base import HealthCheck
from oca.schemas import CheckResult


class EndpointCheck(HealthCheck):
    def __init__(self, runner, service: str, interval_s: int = 10, opens_ticket: bool = True) -> None:
        self.runner, self.service, self.interval_s, self.opens_ticket = runner, service, interval_s, opens_ticket
        self.name = f"endpoint_check:{service}"

    async def run(self) -> CheckResult:
        ok, detail = await self.runner.health(self.service)
        return CheckResult(ok=ok, check=self.name, service=self.service, detail=detail,
                           metrics={"up": 1.0 if ok else 0.0}, breaches=[] if ok else ["endpoint"])
