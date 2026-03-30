"""
Abstract base for all schema providers.
Both FileSchemaProvider and live-DB providers implement this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True
    primary_key: bool = False
    foreign_key_ref: str | None = None   # "schema.table.column"


@dataclass
class TableSchema:
    schema_name: str
    table_name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_estimate: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


@dataclass
class ExplainResult:
    ok: bool
    plan: str          # raw text from DB optimizer
    error: str | None = None
    dialect: str = ""  # "postgres" | "oracle" | "static"


@dataclass
class SampleRow:
    """One row of sample data, already PII-masked."""
    data: dict[str, Any]


class SchemaProvider(ABC):
    """
    Unified interface used by agent tools, FastAPI routes, and the Self-Healing loop.

    All methods are SYNCHRONOUS. Do not add async/await.
    All methods must handle errors internally and never raise unhandled exceptions
    that would crash the FastAPI process — log and return safe defaults instead.
    """

    @abstractmethod
    def ping(self) -> bool:
        """Health check. True = provider reachable."""

    @abstractmethod
    def list_tables(self, schema_filter: str | None = None) -> list[str]:
        """
        Return list of 'schema.table' strings.
        schema_filter: if provided, return only tables in that schema.
        """

    # Alias used by discovery endpoint
    def get_table_list(self, schema_filter: str | None = None) -> list[str]:
        return self.list_tables(schema_filter=schema_filter)

    @abstractmethod
    def get_table_schema(self, table_key: str) -> TableSchema | None:
        """Full column metadata. Returns None if table not found."""

    @abstractmethod
    def get_columns(self, table_key: str) -> list[str]:
        """Column name list only — backward-compat with agent_tools.py."""

    @abstractmethod
    def get_sample_data(self, table_key: str, limit: int = 5) -> list[SampleRow]:
        """
        Return up to `limit` rows of real data from the source table.
        IMPORTANT: data must be PII-masked BEFORE being returned.
        FileSchemaProvider returns [] (no live data available).
        """

    @abstractmethod
    def execute_explain(self, sql: str) -> ExplainResult:
        """
        Run the DB engine's native EXPLAIN on `sql`.
        - Postgres: EXPLAIN (FORMAT JSON) <sql>
        - Oracle:   EXPLAIN PLAN FOR <sql>  +  SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)
        - File:     returns ExplainResult(ok=True, plan="static_validation_only", dialect="static")
        IMPORTANT: use EXPLAIN only — never execute DML.
        """

    def close(self) -> None:
        """Release DB connections. Override in live providers."""

