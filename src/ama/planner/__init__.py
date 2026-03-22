"""
Autonomous Planner — builds migration plans from ingestion reports.

Plans are deterministic, data-driven suggestions (waves by domain / priority),
not automatic production cutovers.
"""

from ama.planner.models import MigrationPlan, MigrationWave, PlannedTable
from ama.planner.planner import AutonomousPlanner

__all__ = [
    "AutonomousPlanner",
    "MigrationPlan",
    "MigrationWave",
    "PlannedTable",
]
