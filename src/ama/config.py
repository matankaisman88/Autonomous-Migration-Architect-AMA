from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_migration_context(migration_context: str) -> tuple[str, str]:
    """Split ``schema.table`` into (schema, table); single segment → ('', table)."""
    mc = (migration_context or "").strip()
    if "." in mc:
        a, b = mc.split(".", 1)
        return a.strip(), b.strip()
    return "", mc


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AMA_", env_file=".env", extra="ignore")

    migration_context: str = Field(
        default="sales.orders",
        description="Qualified schema.table for comms/git anchor and single-table SQL pipeline",
    )
    target_schema: str | None = Field(
        default=None,
        description="Deprecated: use AMA_MIGRATION_CONTEXT; merged when migration_context is unset/default",
        validation_alias=AliasChoices("target_schema", "TARGET_SCHEMA"),
    )
    target_table: str | None = Field(
        default=None,
        description="Deprecated: use AMA_MIGRATION_CONTEXT; merged when migration_context is unset/default",
        validation_alias=AliasChoices("target_table", "TARGET_TABLE"),
    )
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
        description="With discovery mode: merge all discovered tables (per manifest); else top-N or scope-only",
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

    @model_validator(mode="after")
    def _merge_deprecated_target_into_context(self) -> IngestionSettings:
        """If legacy AMA_TARGET_SCHEMA / AMA_TARGET_TABLE are set and context is still default, compose."""
        ts = (self.target_schema or "").strip()
        tt = (self.target_table or "").strip()
        if ts and tt:
            mc = self.migration_context.strip()
            if mc in ("", "sales.orders"):
                return self.model_copy(update={"migration_context": f"{ts}.{tt}"})
        return self

    @property
    def context_schema(self) -> str:
        return split_migration_context(self.migration_context)[0]

    @property
    def context_table(self) -> str:
        return split_migration_context(self.migration_context)[1]

    @property
    def full_table(self) -> str:
        """Qualified name for comms/git filters (same as migration_context when schema.table)."""
        return self.migration_context.strip()


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
