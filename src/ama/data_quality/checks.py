"""DQ check results and severity levels."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DQSeverity(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass
class DQCheckResult:
    """Single named check outcome."""

    name: str
    severity: DQSeverity
    message: str

    def to_dict(self) -> dict:
        return {"name": self.name, "severity": self.severity.value, "message": self.message}


@dataclass
class DQSuiteResult:
    """Aggregate result for CI / CLI."""

    checks: list[DQCheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.severity != DQSeverity.ERROR for c in self.checks)

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == DQSeverity.ERROR)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == DQSeverity.WARN)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_count": self.error_count,
            "warn_count": self.warn_count,
            "checks": [c.to_dict() for c in self.checks],
        }
