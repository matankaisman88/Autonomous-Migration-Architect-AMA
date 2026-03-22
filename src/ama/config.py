from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AMA_", env_file=".env", extra="ignore")

    target_schema: str = Field(default="sales", description="Legacy schema name for focus table")
    target_table: str = Field(default="orders", description="Single table for MVP importance report")
    sql_logs_glob: str = Field(default="**/sql_logs/**/*.jsonl")
    comms_dir: Path = Field(default=Path("sample_data/comms"))
    git_sql_roots: list[Path] = Field(
        default_factory=lambda: [Path("sample_data/git_repo/sql")]
    )
    qdrant_path: Path | None = Field(
        default=None,
        description="If set, persist Qdrant to disk; else in-memory",
    )
    embedding_dim: int = Field(default=64, description="Dimension for hash-based embeddings")
    ddl_columns_path: Path | None = Field(
        default=Path("sample_data/ddl/orders_columns.json"),
        description="If present under data root, merge log columns onto these DDL names by default",
    )
    glossary_path: Path | None = Field(
        default=Path("sample_data/glossary/he_en_columns.json"),
        description="Optional Hebrew/English column glossary for alias merge",
    )
    merge_confidence_floor: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="Below this, log columns are not merged onto DDL (trash/review only)",
    )
    merge_confirmed_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Vector matches must meet this to merge onto DDL (glossary/exact always merge)",
    )
    default_db: str | None = Field(
        default=None,
        description="Catalog/database name when logs only reference schema.table (env: AMA_DEFAULT_DB)",
    )

    @property
    def full_table(self) -> str:
        return f"{self.target_schema}.{self.target_table}"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
