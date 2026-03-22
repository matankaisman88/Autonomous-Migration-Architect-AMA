"""
Log Analysis Engine — streaming analysis of legacy SQL JSONL logs.

Delegates parsing and telemetry to :mod:`ama.sql_pipeline` while exposing a stable
configuration surface for planners and DQ.
"""

from ama.log_analysis.config import LogAnalysisConfig
from ama.log_analysis.engine import LogAnalysisEngine, LogAnalysisSummary

__all__ = [
    "LogAnalysisConfig",
    "LogAnalysisEngine",
    "LogAnalysisSummary",
]
