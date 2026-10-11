from __future__ import annotations

from abc import ABC, abstractmethod

from oca.schemas import CheckResult


class HealthCheck(ABC):
    name: str
    service: str
    interval_s: int
    opens_ticket: bool = True
    enabled_by_default: bool = True

    @abstractmethod
    async def run(self) -> CheckResult: ...


class PlaceholderCheck(HealthCheck):
    """Registered so it shows on the Health page; does nothing until its integration exists."""

    opens_ticket = False

    def __init__(self, name: str, service: str, interval_s: int, enabled: bool, detail: str) -> None:
        self.name, self.service, self.interval_s = name, service, interval_s
        self.enabled_by_default = enabled
        self.detail = detail

    async def run(self) -> CheckResult:
        return CheckResult(ok=True, check=self.name, service=self.service, detail=self.detail)
