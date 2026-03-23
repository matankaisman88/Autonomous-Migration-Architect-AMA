"""
Pydantic configuration for Jira and Confluence plan exports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExportConfig(BaseModel):
    """Settings for writing migration plan exports (file-only, no API calls)."""

    format: Literal["jira", "confluence"]
    project_key: str = "MIG"
    epic_prefix: str = "Wave"
    jira_priority_map: dict[str, str] = Field(
        default_factory=lambda: {"high": "High", "medium": "Medium", "low": "Low"},
    )
    max_description_chars: int = 1500
