"""Data models for migration planning (serializable, report-driven)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PlannedTable:
    """One table scheduled in a wave."""

    full_name: str
    business_domain: str
    priority_score: float
    query_count: int
    rationale: str = ""
    business_context: str = ""
    technical_note: str = ""


@dataclass
class MigrationWave:
    """Ordered group of tables (e.g. same business domain or risk tier)."""

    wave_id: int
    name: str
    tables: list[PlannedTable] = field(default_factory=list)
    business_rationale: str = ""
    technical_rationale: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "wave_id": self.wave_id,
            "name": self.name,
            "business_rationale": self.business_rationale,
            "technical_rationale": self.technical_rationale,
            "metrics": dict(self.metrics),
            "tables": [asdict(t) for t in self.tables],
        }


@dataclass
class MigrationPlan:
    """Full plan artifact (JSON-friendly)."""

    version: str = "1.0"
    source: str = "ama.planner"
    target_focus: str = ""
    waves: list[MigrationWave] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "target_focus": self.target_focus,
            "waves": [w.to_dict() for w in self.waves],
            "notes": list(self.notes),
        }
