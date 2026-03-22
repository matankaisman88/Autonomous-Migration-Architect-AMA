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
        description="Default DDL column list; also fallback for tables not listed in ddl_manifest",
    )
    ddl_manifest_path: Path | None = Field(
        default=Path("sample_data/ddl/ddl_manifest.json"),
        description="Optional JSON map schema.table -> DDL file path relative to data root",
    )
    discovery_merge_all: bool = Field(
        default=False,
        description="With discovery mode: merge all discovered tables (per manifest); else top-N or target-only",
    )
    discovery_merge_max: int = Field(
        default=0,
        ge=0,
        description="When discovery_merge_all: max tables (0 = unlimited)",
    )
    glossary_path: Path | None = Field(
        default=Path("sample_data/glossary/he_en_columns.json"),
        description="Optional Hebrew/English column glossary for alias merge",
    )
    glossary_dirty_path: Path | None = Field(
        default=Path("sample_data/glossary/he_en_columns_dirty.json"),
        description="Optional second glossary (typos/shorthand); merged after glossary_path, first file wins on key clash",
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
    default_sql_dialect: str | None = Field(
        default=None,
        description="Fallback SQLGlot dialect when JSONL rows omit dialect (env: AMA_DEFAULT_SQL_DIALECT)",
    )

    @property
    def full_table(self) -> str:
        return f"{self.target_schema}.{self.target_table}"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
