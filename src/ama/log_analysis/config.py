"""Configuration for the Log Analysis Engine (env filters, limits, dialect defaults)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LogAnalysisConfig(BaseModel):
    """
    Controls how SQL JSONL files are scanned.

    Paths are resolved by the caller; use :func:`ama.security.credentials.ensure_under_root`
    when joining untrusted input to a data root.
    """

    model_config = ConfigDict(extra="ignore")

    env_filter: str | None = Field(
        default="prod",
        description="If set, only ingest rows whose `env` field matches (case-insensitive). "
        "Use empty string or None to disable.",
    )
    default_sql_dialect: str | None = Field(
        default=None,
        description="Fallback dialect when a JSONL row omits `dialect` (e.g. `tsql`, `postgres`).",
    )
    max_records_per_file: int | None = Field(
        default=None,
        ge=1,
        description="Stop after this many valid JSON records per file (None = entire file, streaming).",
    )
    progress_every: int = Field(
        default=50_000,
        ge=1,
        description="Emit stderr progress every N records when progress=True in the engine.",
    )
    progress_chunk_every: int = Field(
        default=20,
        ge=1,
        description="Emit chunk-level progress every N processed chunks when progress=True.",
    )
    chunk_size: int = Field(
        default=5000,
        ge=100,
        description="Rows processed per chunk for incremental co-occurrence updates.",
    )
    sparse_density_threshold: float = Field(
        default=0.15,
        gt=0.0,
        le=1.0,
        description="Use sparse similarity implementation when co-occurrence density <= threshold.",
    )

    def effective_env(self) -> str | None:
        """Normalize env filter: empty string means no filter."""
        if self.env_filter is None:
            return None
        s = str(self.env_filter).strip()
        return None if s == "" else s
