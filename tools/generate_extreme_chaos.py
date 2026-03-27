"""
Generate high-scale, multi-dialect chaos assets (DDL + JSONL SQL logs).

The generator streams log rows to disk and does not keep all rows in memory.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "chaos_data" / "sql_logs" / "extreme_1m.jsonl"
SUPPORTED_DIALECTS = ("sqlserver", "oracle", "db2")
DEFAULT_SCHEMAS = ("FINANCE_CORE", "SALES_CORE", "LEGACY_EDGE")
DEFAULT_DATABASES = ("CORE_DB", "EDGE_DB", "SHARED_DB")


@dataclass(frozen=True)
class TableRef:
    database: str
    schema: str
    table: str

    @property
    def key(self) -> str:
        return f"{self.database}.{self.schema}.{self.table}"

    def sql_name(self, dialect: str) -> str:
        if dialect == "oracle":
            return f"{self.schema}.{self.table}"
        if dialect == "db2":
            return f"{self.schema}.{self.table}"
        return self.key

    def sequence_name(self) -> str:
        return f"{self.schema}.{self.table}_SEQ"


class ChaosFactory:
    """
    High-scale synthetic chaos asset generator.

    - `scale`: number of logical tables.
    - `source_dialect`: sqlserver, oracle, db2
    """

    def __init__(
        self,
        *,
        scale: int = 1000,
        source_dialect: str = "sqlserver",
        join_width: int = 10,
        select_columns: int = 24,
        schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
        databases: tuple[str, ...] = DEFAULT_DATABASES,
    ) -> None:
        self.scale = max(1, int(scale))
        self.source_dialect = source_dialect.strip().lower()
        if self.source_dialect not in SUPPORTED_DIALECTS:
            raise ValueError(
                f"Unsupported --source-dialect {source_dialect!r}; expected one of: {', '.join(SUPPORTED_DIALECTS)}"
            )
        self.join_width = max(1, int(join_width))
        self.select_columns = max(1, int(select_columns))
        self._schemas = schemas
        self._databases = databases
        self._tables = self._build_tables()

    def _build_tables(self) -> list[TableRef]:
        refs: list[TableRef] = []
        for i in range(self.scale):
            db = self._databases[i % len(self._databases)]
            schema = self._schemas[i % len(self._schemas)]
            tname = f"TBL_{i + 1:05d}"
            refs.append(TableRef(database=db, schema=schema, table=tname))
        return refs

    def _ddl_column_block(self, idx: int) -> list[str]:
        d = self.source_dialect
        if d == "oracle":
            return [
                "  ID NUMBER(19) PRIMARY KEY",
                "  PARENT_ID NUMBER(19)",
                "  STATUS VARCHAR2(20)",
                "  PAYLOAD CLOB",
                "  CREATED_AT TIMESTAMP",
                f"  SHARD_KEY NUMBER(6) DEFAULT {idx % 997}",
            ]
        if d == "db2":
            return [
                "  ID BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                "  PARENT_ID BIGINT",
                "  STATUS VARCHAR(20)",
                "  PAYLOAD CLOB(2M)",
                "  CREATED_AT TIMESTAMP",
                f"  SHARD_KEY INTEGER DEFAULT {idx % 997}",
            ]
        return [
            "  ID BIGINT IDENTITY(1,1) PRIMARY KEY",
            "  PARENT_ID BIGINT",
            "  STATUS NVARCHAR(20)",
            "  PAYLOAD NVARCHAR(MAX)",
            "  CREATED_AT DATETIME2",
            f"  SHARD_KEY INT DEFAULT {idx % 997}",
        ]

    def ddl_statements(self) -> list[str]:
        stmts: list[str] = []
        for idx, ref in enumerate(self._tables):
            q = ref.sql_name(self.source_dialect)
            cols = ",\n".join(self._ddl_column_block(idx))
            if self.source_dialect == "oracle":
                stmts.append(f"CREATE SEQUENCE {ref.sequence_name()} START WITH 1 INCREMENT BY 1;")
                stmts.append(
                    "\n".join(
                        [
                            f"CREATE TABLE {q} (",
                            cols,
                            f") TABLESPACE TS_{idx % 3};",
                        ]
                    )
                )
            elif self.source_dialect == "db2":
                stmts.append(
                    "\n".join(
                        [
                            f"CREATE TABLE {q} (",
                            cols,
                            f") IN TS_{idx % 3} ORGANIZE BY ROW;",
                        ]
                    )
                )
            else:
                stmts.append(
                    "\n".join(
                        [
                            f"CREATE TABLE {q} (",
                            cols,
                            ");",
                        ]
                    )
                )
        return stmts

    def _complex_select(self, idx: int) -> str:
        base = self._tables[idx % len(self._tables)]
        join_refs = [self._tables[(idx + j) % len(self._tables)] for j in range(1, self.join_width + 1)]
        cols = ", ".join(f"c_{k}_{idx % 500}" for k in range(self.select_columns))
        nested = (
            f"(SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM {base.sql_name(self.source_dialect)} ni "
            f"WHERE ni.SHARD_KEY = {idx % 997} GROUP BY ni.STATUS) agg)"
        )
        joins = " ".join(
            f"INNER JOIN {jr.sql_name(self.source_dialect)} t{j} "
            f"ON t0.ID = t{j}.PARENT_ID AND t{j}.SHARD_KEY = {idx % 997}"
            for j, jr in enumerate(join_refs, 1)
        )
        return (
            f"SELECT {cols}, {nested} AS nested_metric "
            f"FROM {base.sql_name(self.source_dialect)} t0 {joins} "
            f"WHERE t0.STATUS IN ('A','B','C') AND t0.SHARD_KEY = {idx % 997}"
        )

    def _insert_sql(self, idx: int) -> str:
        ref = self._tables[idx % len(self._tables)]
        q = ref.sql_name(self.source_dialect)
        payload = f"payload_{idx % 10000}"
        if self.source_dialect == "oracle":
            return (
                f"INSERT INTO {q} (ID, PARENT_ID, STATUS, PAYLOAD, CREATED_AT, SHARD_KEY) "
                f"VALUES ({ref.sequence_name()}.NEXTVAL, {idx % 2500}, 'A', '{payload}', SYSTIMESTAMP, {idx % 997})"
            )
        if self.source_dialect == "db2":
            return (
                f"INSERT INTO {q} (PARENT_ID, STATUS, PAYLOAD, CREATED_AT, SHARD_KEY) "
                f"VALUES ({idx % 2500}, 'A', '{payload}', CURRENT TIMESTAMP, {idx % 997})"
            )
        return (
            f"INSERT INTO {q} (PARENT_ID, STATUS, PAYLOAD, CREATED_AT, SHARD_KEY) "
            f"VALUES ({idx % 2500}, 'A', '{payload}', SYSUTCDATETIME(), {idx % 997})"
        )

    def _log_row(self, idx: int) -> str:
        if idx % 7 == 0:
            sql = self._insert_sql(idx)
        else:
            sql = self._complex_select(idx)
        rec = {
            "env": "prod",
            "dialect": self.source_dialect,
            "source_dialect": self.source_dialect,
            "sql": sql,
            "table_key": self._tables[idx % len(self._tables)].key,
            "batch_id": idx // 10_000,
            "chunk_id": idx // 5_000,
        }
        return json.dumps(rec, ensure_ascii=False)

    def stream_logs(self, *, lines: int, out_jsonl: Path) -> int:
        out_jsonl.parent.mkdir(parents=True, exist_ok=True)
        n = max(1, int(lines))
        written = 0
        with out_jsonl.open("w", encoding="utf-8", newline="\n") as f:
            for i in range(n):
                f.write(self._log_row(i))
                f.write("\n")
                written += 1
                if written % 100_000 == 0:
                    print(f"  ... {written:,} lines", flush=True)
        return written

    def write_ddl(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        ddl = "\n\n".join(self.ddl_statements()) + "\n"
        path.write_text(ddl, encoding="utf-8")
        return len(self._tables)

    def write_manifest(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        by_schema: dict[str, int] = {}
        by_database: dict[str, int] = {}
        for t in self._tables:
            by_schema[t.schema] = by_schema.get(t.schema, 0) + 1
            by_database[t.database] = by_database.get(t.database, 0) + 1
        payload = {
            "_meta": {
                "source_dialect": self.source_dialect,
                "scale": self.scale,
                "schemas": list(self._schemas),
                "databases": list(self._databases),
                "tables_per_schema": by_schema,
                "tables_per_database": by_database,
            },
            "tables": [t.key for t in self._tables],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description="Generate extreme multi-source chaos assets.")
    p.add_argument("--lines", type=int, default=1_000_000, help="Number of JSONL rows (default: 1,000,000)")
    p.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT),
        help="JSONL output path",
    )
    p.add_argument(
        "--scale",
        type=int,
        default=1_000,
        help="Number of generated logical tables (default: 1,000)",
    )
    p.add_argument(
        "--source-dialect",
        type=str,
        default="sqlserver",
        choices=list(SUPPORTED_DIALECTS),
        help="Source SQL dialect for generated DDL/logs",
    )
    p.add_argument(
        "--join-width",
        type=int,
        default=10,
        help="Number of joined tables per SELECT query (default: 10)",
    )
    p.add_argument(
        "--select-columns",
        type=int,
        default=24,
        help="Number of selected synthetic columns per SELECT query (default: 24)",
    )
    p.add_argument(
        "--ddl-out",
        type=str,
        default=str(ROOT / "chaos_data" / "ddl" / "extreme_chaos_ddl.sql"),
        help="DDL output path",
    )
    p.add_argument(
        "--manifest-out",
        type=str,
        default=str(ROOT / "chaos_data" / "ddl" / "extreme_chaos_manifest.json"),
        help="Table distribution manifest output path",
    )
    args = p.parse_args()

    factory = ChaosFactory(
        scale=args.scale,
        source_dialect=args.source_dialect,
        join_width=args.join_width,
        select_columns=args.select_columns,
    )
    out = Path(args.out).resolve()
    ddl_out = Path(args.ddl_out).resolve()
    manifest_out = Path(args.manifest_out).resolve()

    ddl_tables = factory.write_ddl(ddl_out)
    factory.write_manifest(manifest_out)
    written = factory.stream_logs(lines=args.lines, out_jsonl=out)

    print(f"Wrote {ddl_tables:,} table DDL statements to {ddl_out}")
    print(f"Wrote table manifest to {manifest_out}")
    print(f"Wrote {written:,} JSONL rows to {out}")


if __name__ == "__main__":
    main()
