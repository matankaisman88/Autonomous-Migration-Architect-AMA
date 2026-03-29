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
        chaos = idx % 3
        
        if d == "oracle":
            cols = ["  ID NUMBER(19) PRIMARY KEY", "  PARENT_ID NUMBER(19)", "  STATUS VARCHAR2(20)", f"  SHARD_KEY NUMBER(6) DEFAULT {idx % 997}"]
            pay, cre = "  PAYLOAD CLOB", "  CREATED_AT TIMESTAMP"
        elif d == "db2":
            cols = ["  ID BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "  PARENT_ID BIGINT", "  STATUS VARCHAR(20)", f"  SHARD_KEY INTEGER DEFAULT {idx % 997}"]
            pay, cre = "  PAYLOAD CLOB(2M)", "  CREATED_AT TIMESTAMP"
        else:
            cols = ["  ID BIGINT IDENTITY(1,1) PRIMARY KEY", "  PARENT_ID BIGINT", "  STATUS NVARCHAR(20)", f"  SHARD_KEY INT DEFAULT {idx % 997}"]
            pay, cre = "  PAYLOAD NVARCHAR(MAX)", "  CREATED_AT DATETIME2"

        if chaos == 0:
            cols.extend([pay, cre])
        elif chaos == 1:
            cols.extend([pay, cre, "  OBSOLETE_COL INT"])
        else:
            cols = ["  RANDOM_COL_" + str(i) + " INT" for i in range(5)]
            
        return cols

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
        
        cols_list = [f"c_{k}_{idx % 500}" for k in range(self.select_columns)]
        chaos = idx % 3
        
        if chaos == 1: 
            cols_list.append("UNKNOWN_YELLOW_COL")
        elif chaos == 2: 
            cols_list = ["USER_ID", "SESSION_TOKEN", "ACTION_CODE"]
            cols = ", ".join(cols_list)
            return f"SELECT {cols} FROM {base.sql_name(self.source_dialect)} t0 WHERE t0.SHARD_KEY = {idx % 997}"
            
        cols = ", ".join(cols_list)
        nested = f"(SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM {base.sql_name(self.source_dialect)} ni WHERE ni.SHARD_KEY = {idx % 997} GROUP BY ni.STATUS) agg)"
        joins = " ".join(f"INNER JOIN {jr.sql_name(self.source_dialect)} t{j} ON t0.ID = t{j}.PARENT_ID AND t{j}.SHARD_KEY = {idx % 997}" for j, jr in enumerate(join_refs, 1))
        
        return f"SELECT {cols}, {nested} AS nested_metric FROM {base.sql_name(self.source_dialect)} t0 {joins} WHERE t0.STATUS IN ('A','B','C') AND t0.SHARD_KEY = {idx % 997}"

    def _insert_sql(self, idx: int) -> str:
        ref = self._tables[idx % len(self._tables)]
        q = ref.sql_name(self.source_dialect)
        c = idx % 3
        
        if c == 0:
            cols = "(PARENT_ID, STATUS, PAYLOAD, CREATED_AT, SHARD_KEY)"
            vals = f"({idx % 2500}, 'A', 'data', CURRENT_TIMESTAMP, {idx % 997})"
        elif c == 1:
            cols = "(PARENT_ID, STATUS, PAYLOAD, LEGACY_REMARK, SHARD_KEY)"
            vals = f"({idx % 2500}, 'A', 'data', 'old', {idx % 997})"
        else:
            cols = "(USER_ID, SESSION_TOKEN, ACTION_CODE, APP_VERSION)"
            vals = f"({idx}, 'token_{idx}', 'LOGIN', '1.0.0')"
            
        if self.source_dialect == "oracle":
            if c != 2:
                cols = cols.replace("(", "(ID, ")
                vals = vals.replace("CURRENT_TIMESTAMP", "SYSTIMESTAMP").replace("(", f"({ref.sequence_name()}.NEXTVAL, ")
        elif self.source_dialect == "sqlserver":
            vals = vals.replace("CURRENT_TIMESTAMP", "SYSUTCDATETIME()")
        elif self.source_dialect == "db2":
            vals = vals.replace("CURRENT_TIMESTAMP", "CURRENT TIMESTAMP")
            
        return f"INSERT INTO {q} {cols} VALUES {vals}"

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

    def write_manifest(self, path: Path, ddl_rel_path: str) -> None:
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
            "tables": {t.key: ddl_rel_path for t in self._tables},
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lines", type=int, default=1_000_000)
    p.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    p.add_argument("--scale", type=int, default=1_000)
    p.add_argument("--source-dialect", type=str, default="sqlserver", choices=list(SUPPORTED_DIALECTS))
    p.add_argument("--join-width", type=int, default=10)
    p.add_argument("--select-columns", type=int, default=24)
    p.add_argument("--ddl-out", type=str, default=str(ROOT / "chaos_data" / "ddl" / "extreme_chaos_ddl.sql"))
    p.add_argument("--manifest-out", type=str, default=str(ROOT / "chaos_data" / "ddl" / "extreme_chaos_manifest.json"))
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

    ddl_rel = str(ddl_out.relative_to(ROOT)) if ddl_out.is_relative_to(ROOT) else str(ddl_out)

    ddl_tables = factory.write_ddl(ddl_out)
    factory.write_manifest(manifest_out, ddl_rel)
    written = factory.stream_logs(lines=args.lines, out_jsonl=out)

    print(f"Wrote {ddl_tables:,} table DDL statements to {ddl_out}")
    print(f"Wrote table manifest to {manifest_out}")
    print(f"Wrote {written:,} JSONL rows to {out}")

if __name__ == "__main__":
    main()