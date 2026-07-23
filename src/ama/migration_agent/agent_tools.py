from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ama.dbt_migration.generator import generate_model_artifact
from ama.dbt_migration.models import CheckpointAArtifact, TargetDialect
from ama.dbt_migration.sql_self_heal import validate_sql_with_sqlglot
from ama.dbt_migration.runner import _run_command, _run_fix_agent
from ama.dbt_migration.sql_transpile import validate_target_dialect
from ama.hitl_apply import decision_from_queue
from ama.planner import AutonomousPlanner
from ama.scale_engine.anomaly import AnomalyFlag
from ama.scale_engine.audit import append_decision
from ama.scale_engine.contract import MigrationContract
from ama.scale_engine.criticality import CriticalityResult
from ama.scale_engine import evaluate_batch
from ama.scale_engine.scorer import ConfidenceResult


def _synthetic_value_for_column(column: str) -> str:
    c = str(column or "").lower()
    if c.endswith("_id") or c == "id":
        return "1001"
    if "amount" in c or "total" in c or "rate" in c or "price" in c:
        return "123.45"
    if "date" in c or c.endswith("_at") or "time" in c:
        return "2026-01-15T10:30:00Z"
    if "status" in c:
        return "active"
    if "currency" in c:
        return "USD"
    return "sample"


def _infer_column_type(column: str) -> str:
    c = str(column or "").lower()
    if c.endswith("_id") or c == "id":
        return "id"
    if "amount" in c or "total" in c or "rate" in c or "price" in c or "qty" in c or "count" in c:
        return "numeric"
    if "date" in c or c.endswith("_at") or "time" in c:
        return "timestamp"
    if "status" in c or "state" in c:
        return "status/text"
    if "currency" in c:
        return "currency/text"
    if "flag" in c or c.startswith("is_") or c.startswith("has_"):
        return "boolean-like"
    return "text"

def get_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_waves",
                "description": "Fetch migration waves and tables from AMA report.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_schema",
                "description": "Return DDL columns and sample rows for a table.",
                "parameters": {
                    "type": "object",
                    "properties": {"table": {"type": "string"}},
                    "required": ["table"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_dbt_model",
                "description": "Generate candidate dbt SQL and reasoning for a table.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "dialect": {"type": "string"},
                    },
                    "required": ["table", "dialect"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "execute_dbt_test",
                "description": "Run dbt run + dbt test for a model and return logs.",
                "parameters": {
                    "type": "object",
                    "properties": {"model": {"type": "string"}},
                    "required": ["model"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_fix",
                "description": "Run fix agent and return corrected SQL + analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "error_log": {"type": "string"},
                    },
                    "required": ["model", "error_log"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "request_write_permission",
                "description": "Protected write gate. Returns pending_write payload only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "sql": {"type": "string"},
                    },
                    "required": ["model", "sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_synthetic_rows",
                "description": "Generate mock rows for a given source table using schema evidence (and synthetic fallbacks).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "row_count": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["table"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "validate_sql_on_duckdb",
                "description": "Validate SQL syntax using SQLGlot (dbt/Jinja blocks are stripped). Intended for fast pre-approval QA.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string"},
                        "dialect": {
                            "type": "string",
                            "description": "Target dialect (duckdb|snowflake|bigquery|redshift). If omitted, defaults to duckdb.",
                        },
                    },
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_live_tables",
                "description": (
                    "List all tables from the live connected database (Postgres or Oracle). "
                    "Use this instead of analyze_schema when you need to discover what tables exist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schema_filter": {
                            "type": "string",
                            "description": "Optional schema/owner name to filter results.",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "explain_sql_live",
                "description": (
                    "Run the DB engine's native EXPLAIN on a SQL statement. "
                    "Returns the optimizer plan and any errors. "
                    "Always call this before execute_dbt_test in Self-Healing mode."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "SQL to validate with EXPLAIN."},
                    },
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_inventory",
                "description": "Filter and sort scored inventory tables.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filters": {"type": "object"},
                        "sort_by": {"type": "string"},
                        "sort_order": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bulk_migrate_tables",
                "description": "Bulk migrate tables through scale-engine queueing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filters": {"type": "object"},
                        "dialect": {"type": "string"},
                        "dry_run": {"type": "boolean"},
                    },
                    "required": ["filters", "dialect"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "explain_table_score",
                "description": "Explain score and queue for a table.",
                "parameters": {
                    "type": "object",
                    "properties": {"table_key": {"type": "string"}},
                    "required": ["table_key"],
                },
            },
        },
    ]


def _extract_inventory(report: dict[str, Any]) -> list[dict[str, Any]]:
    discovery = report.get("discovery")
    if not isinstance(discovery, dict):
        return []
    inventory = discovery.get("inventory")
    if not isinstance(inventory, list):
        return []
    return [row for row in inventory if isinstance(row, dict)]


def _extract_columns_for_table(report: dict[str, Any], table_key: str) -> list[str]:
    out: list[str] = []
    importance = report.get("importance_ddl")
    if isinstance(importance, list):
        for row in importance:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_table") or "").strip() != table_key:
                continue
            col = str(row.get("column") or "").strip()
            if col:
                out.append(col)
    if out:
        return list(dict.fromkeys(out))
    # Live-extraction reports store per-column rows as "schema.table::column".
    prefix = f"{table_key}::"
    for row in report.get("columns") or []:
        if not isinstance(row, dict):
            continue
        col_key = str(row.get("column") or "").strip()
        if col_key.startswith(prefix):
            out.append(col_key.split("::", 1)[1])
    return list(dict.fromkeys(out))


def _ddl_columns_from_live_data_artifacts(report_path: Path, table_key: str) -> list[str]:
    """Load DDL column list from ``live_data/<conn>/ddl/<schema>_<table>.json`` when present."""
    root = report_path.parent
    if "." in table_key:
        schema_name, table_name = table_key.split(".", 1)
    else:
        schema_name, table_name = "dbo", table_key
    ddl_path = root / "ddl" / f"{schema_name}_{table_name}.json"
    if not ddl_path.is_file():
        return []
    try:
        payload = json.loads(ddl_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cols = payload.get("columns") if isinstance(payload, dict) else None
    if not isinstance(cols, list):
        return []
    return [str(c).strip() for c in cols if str(c).strip()]


def _columns_from_schema_yml(schema_yml: str) -> list[str]:
    names = re.findall(r"^\s*-\s*name:\s*(\S+)\s*$", str(schema_yml or ""), flags=re.MULTILINE)
    return list(dict.fromkeys(names))


def _resolve_bootstrap_columns(
    *,
    report: dict[str, Any] | None,
    report_path: Path | None,
    table_key: str,
    schema_yml: str | None = None,
    sql_text: str | None = None,
) -> list[str]:
    """
    Resolve real DDL column names for DuckDB source bootstrap (approve-time validation).

    Merges all available sources (union) so log-sparse importance_ddl does not omit columns
    that appear in manifest DDL, schema.yml, or the model SQL.
    """
    merged: list[str] = []

    def _add(items: list[str]) -> None:
        for col in items:
            name = str(col).strip()
            if name and name not in merged:
                merged.append(name)

    if report is not None:
        _add(_extract_columns_for_table(report, table_key))
        if report_path is not None:
            manifest_cols = _load_manifest_table_columns(report, report_path)
            _add(manifest_cols.get(table_key, []))
    if report_path is not None:
        _add(_ddl_columns_from_live_data_artifacts(report_path, table_key))
    if schema_yml:
        _add(_columns_from_schema_yml(schema_yml))
    if sql_text:
        _add(_extract_source_columns(sql_text))
    return merged


    return merged


def _duckdb_table_columns(con: Any, schema_name: str, table_name: str) -> list[str]:
    try:
        rows = con.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [schema_name, table_name],
        ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _merge_column_names(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for col in group:
            name = str(col).strip()
            if name and name not in merged:
                merged.append(name)
    return merged


def collect_source_columns_for_artifacts(
    *,
    artifacts: list[Any],
    report: dict[str, Any] | None,
    report_path: Path | None,
) -> dict[str, list[str]]:
    """
    Union source-table columns referenced across a batch of generated model artifacts.

    Uses Checkpoint-A SQL + schema.yml plus report/manifest DDL so bootstrap tables include
    every column any model in the batch may SELECT — not only columns seen in SQL logs.
    """
    per_source: dict[str, list[str]] = {}

    def _add(table_key: str, cols: list[str]) -> None:
        if table_key not in per_source:
            per_source[table_key] = []
        for col in cols:
            if col and col not in per_source[table_key]:
                per_source[table_key].append(col)

    for artifact in artifacts:
        sql = str(getattr(artifact, "sql", "") or "")
        table_key = str(getattr(artifact, "table_key", "") or "").strip()
        schema_yml = str(getattr(artifact, "schema_yml", "") or "")
        for schema_name, table_name in _extract_source_relations(sql):
            rel_key = f"{schema_name}.{table_name}"
            use_schema = schema_yml if table_key and rel_key == table_key else None
            _add(
                rel_key,
                _resolve_bootstrap_columns(
                    report=report,
                    report_path=report_path,
                    table_key=rel_key,
                    schema_yml=use_schema,
                    sql_text=sql,
                ),
            )
        if table_key:
            _add(table_key, _columns_from_schema_yml(schema_yml))
            _add(
                table_key,
                _resolve_bootstrap_columns(
                    report=report,
                    report_path=report_path,
                    table_key=table_key,
                    schema_yml=schema_yml,
                    sql_text=sql,
                ),
            )
    return per_source


def bootstrap_duckdb_sources_for_artifacts(
    *,
    dbt_project_dir: Path,
    artifacts: list[Any],
    report: dict[str, Any] | None = None,
    report_path: Path | None = None,
) -> int:
    """Create/refresh DuckDB stub source tables for all relations referenced by a model batch."""
    source_columns = collect_source_columns_for_artifacts(
        artifacts=artifacts,
        report=report,
        report_path=report_path,
    )
    if not source_columns:
        return 0
    target_dir = dbt_project_dir / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "duckdb.db"
    try:
        import duckdb  # type: ignore
    except Exception:
        return 0
    created = 0
    try:
        con = duckdb.connect(str(db_path))
    except Exception:
        return 0
    try:
        for table_key, columns in source_columns.items():
            if "." in table_key:
                schema_name, table_name = table_key.split(".", 1)
            else:
                schema_name, table_name = "dbo", table_key
            merged = _merge_column_names(
                columns,
                _duckdb_table_columns(con, schema_name, table_name),
            )
            if not merged:
                merged = [f"ds_col_{i}" for i in range(1, 11)]
            col_def = ", ".join(f'"{c}" VARCHAR' for c in merged)
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
                con.execute(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}"')
                con.execute(f'CREATE TABLE "{schema_name}"."{table_name}" ({col_def})')
                created += 1
            except Exception:
                continue
    finally:
        con.close()
    return created


def bootstrap_duckdb_sources_for_checkpoint(
    *,
    dbt_project_dir: Path,
    checkpoint: CheckpointAArtifact,
    report: dict[str, Any] | None = None,
    report_path: Path | None = None,
) -> int:
    return bootstrap_duckdb_sources_for_artifacts(
        dbt_project_dir=dbt_project_dir,
        artifacts=checkpoint.generated_models,
        report=report,
        report_path=report_path,
    )


def _load_manifest_table_columns(
    report: dict[str, Any],
    report_path: Path,
    schema_provider=None,  # NEW: SchemaProvider | None
) -> dict[str, list[str]]:
    # Fast path: live provider knows all tables already
    if schema_provider is not None:
        try:
            tables = schema_provider.list_tables()
            return {t: schema_provider.get_columns(t) for t in tables}
        except Exception:
            pass  # fall through to file-based path

    alias_merge = report.get("alias_merge")
    if not isinstance(alias_merge, dict):
        return {}
    manifest_path_raw = alias_merge.get("ddl_manifest")
    if not manifest_path_raw:
        return {}
    manifest_path = Path(str(manifest_path_raw)).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (report_path.parent / manifest_path).resolve()
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    out: dict[str, list[str]] = {}
    for table_key, ddl_file_raw in manifest.items():
        if str(table_key).startswith("_"):
            continue
        ddl_file = Path(str(ddl_file_raw)).expanduser()
        if not ddl_file.is_absolute():
            ddl_file = (report_path.parent / ddl_file).resolve()
        if not ddl_file.is_file():
            continue
        try:
            ddl_payload = json.loads(ddl_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cols = ddl_payload.get("columns") if isinstance(ddl_payload, dict) else None
        if isinstance(cols, list):
            out[str(table_key)] = [str(c).strip() for c in cols if str(c).strip()]
    return out


def _table_rationale(report: dict[str, Any], table_key: str) -> str:
    for row in _extract_inventory(report):
        if str(row.get("full_name") or "").strip() == table_key:
            return str(row.get("rationale") or row.get("reason") or row.get("business_description") or "")
    return ""


def _try_sample_rows_duckdb(*, duckdb_path: Path, table_key: str, sample_row_cap: int) -> tuple[list[dict[str, Any]], str]:
    try:
        import duckdb  # type: ignore
    except Exception:
        return [], "duckdb python package is unavailable in this environment."

    if not duckdb_path.is_file():
        return [], f"DuckDB file not found: {duckdb_path}"
    schema = ""
    table = table_key
    if "." in table_key:
        schema, table = table_key.split(".", 1)
    try:
        con = duckdb.connect(str(duckdb_path), read_only=True)
    except Exception as exc:
        return [], f"failed to open DuckDB file: {exc}"
    try:
        if schema:
            q = f'SELECT * FROM "{schema}"."{table}" LIMIT {int(sample_row_cap)}'
        else:
            q = f'SELECT * FROM "{table}" LIMIT {int(sample_row_cap)}'
        rows = con.execute(q).fetchall()
        cols = [str(x[0]) for x in con.description or []]
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({cols[i]: row[i] for i in range(min(len(cols), len(row)))})
        return out, ""
    except Exception as exc:
        return [], f"duckdb query failed: {exc}"
    finally:
        con.close()


def get_report_summary(*, report: dict[str, Any], model_status_by_name: dict[str, str] | None = None) -> dict[str, Any]:
    plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=50)
    statuses = model_status_by_name or {}
    waves: list[dict[str, Any]] = []
    for wave in plan.waves:
        models = [str(t.full_name).replace(".", "_") for t in wave.tables]
        wave_statuses = [statuses.get(m, "PENDING") for m in models]
        if any(s == "HITL_REQUIRED" for s in wave_statuses):
            wave_state = "HITL_REQUIRED"
        elif models and all(s == "SUCCESS" for s in wave_statuses):
            wave_state = "SUCCESS"
        else:
            wave_state = "PENDING"
        waves.append(
            {
                "wave_id": int(wave.wave_id),
                "name": str(wave.name),
                "tables": [str(t.full_name) for t in wave.tables],
                "model_names": models,
                "status": wave_state,
            }
        )
    return {
        "migration_context": str(report.get("migration_context") or ""),
        "waves": waves,
        "notes": list(plan.notes),
    }


def inspect_table(
    *,
    report: dict[str, Any],
    report_path: Path,
    table_key: str,
    duckdb_path: Path,
    sample_row_cap: int = 10,
) -> dict[str, Any]:
    table_key = str(table_key).strip()
    manifest_cols = _load_manifest_table_columns(report, report_path).get(table_key, [])
    observed_cols = _extract_columns_for_table(report, table_key)
    sample_rows, sample_error = _try_sample_rows_duckdb(
        duckdb_path=duckdb_path,
        table_key=table_key,
        sample_row_cap=max(1, int(sample_row_cap)),
    )
    has_structural_metadata = bool(manifest_cols or observed_cols)
    payload: dict[str, Any] = {
        "table_key": table_key,
        "ddl_columns": manifest_cols,
        "observed_columns": observed_cols,
        "sample_rows": sample_rows,
        "sample_rows_available": not bool(sample_error),
        "non_blocking": has_structural_metadata,
    }
    if sample_error:
        # Degrade gracefully: missing DuckDB data should not block SQL generation
        # if we still have DDL/observed metadata from the report artifacts.
        payload["sample_rows_warning"] = sample_error
        err_low = str(sample_error).lower()
        if "does not exist" in err_low and ("schema" in err_low or "table" in err_low):
            payload["sample_rows_warning_kind"] = "source_table_missing"
        else:
            payload["sample_rows_warning_kind"] = "duckdb_query_error"
        if not has_structural_metadata:
            payload["error"] = sample_error
    return payload


def generate_sql(
    *,
    report: dict[str, Any],
    report_path: Path,
    table_key: str,
    dialect: str,
    glossary_path: Path | None,
) -> dict[str, Any]:
    target_dialect: TargetDialect = validate_target_dialect(dialect)
    raw_columns = _extract_columns_for_table(report, table_key)
    manifest_cols = _load_manifest_table_columns(report, report_path).get(table_key, [])
    glossary: dict[str, str] = {}
    if glossary_path and glossary_path.is_file():
        try:
            loaded = json.loads(glossary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                glossary = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            glossary = {}
    artifact, _mapped = generate_model_artifact(
        table_key=table_key,
        raw_columns=raw_columns,
        glossary=glossary,
        alias_registry={},
        target_dialect=target_dialect,
        source_ddl_columns=manifest_cols,
        broken=False,
        rationale=_table_rationale(report, table_key),
    )
    tokens_used = sum(int(t.get("tokens_used") or 0) for t in artifact.ai_telemetry or [])
    return {
        "table_key": table_key,
        "model_name": artifact.model_name,
        "sql": artifact.sql,
        "schema_yml": artifact.schema_yml,
        "generation_mode": artifact.generation_mode,
        "generation_confidence": artifact.generation_confidence,
        "schema_agent_reasoning": artifact.schema_agent_reasoning,
        "dbt_agent_reasoning": artifact.dbt_agent_reasoning,
        "translation_rationale": artifact.translation_rationale,
        "mapping_rows": [m.model_dump(mode="json") for m in artifact.mapping_rows],
        "tokens_used": tokens_used,
        "ai_telemetry": artifact.ai_telemetry,
    }


def test_model(
    *,
    dbt_project_dir: Path,
    model_name: str,
    target: str | None = None,
    report: dict[str, Any] | None = None,
    report_path: Path | None = None,
    primary_table_key: str | None = None,
    schema_yml: str | None = None,
) -> dict[str, Any]:
    # Important: `dbt test` can return success when a model has zero tests.
    # Run `dbt run` first so invalid SQL is caught before test-only pass states.
    requested_target = str(target or "").strip()
    _sanitize_schema_descriptions(dbt_project_dir=dbt_project_dir)
    _sanitize_generated_sql_files(dbt_project_dir=dbt_project_dir)
    _ensure_duckdb_sources_for_model(
        dbt_project_dir=dbt_project_dir,
        model_name=model_name,
        report=report,
        report_path=report_path,
        primary_table_key=primary_table_key,
        schema_yml=schema_yml,
    )
    run_cmd = ["dbt", "run", "--select", model_name]
    if requested_target:
        run_cmd.extend(["--target", requested_target])
    run_rc, run_out, run_err = _run_command(run_cmd, dbt_project_dir)
    run_logs = (run_err or run_out or "").strip()
    run_rc, run_logs = _retry_on_duckdb_lock(
        dbt_project_dir=dbt_project_dir,
        command=run_cmd,
        initial_rc=run_rc,
        initial_logs=run_logs,
    )
    target_fallback_used = False
    if (
        run_rc != 0
        and requested_target
        and "does not have a target named" in run_logs.lower()
    ):
        # Dashboard may request a target that is valid as SQL dialect but not configured in profiles.yml.
        # Fallback to profile default target for execution so user flow remains non-blocking.
        target_fallback_used = True
        run_cmd = ["dbt", "run", "--select", model_name]
        run_rc, run_out, run_err = _run_command(run_cmd, dbt_project_dir)
        run_logs = (run_err or run_out or "").strip()
        run_rc, run_logs = _retry_on_duckdb_lock(
            dbt_project_dir=dbt_project_dir,
            command=run_cmd,
            initial_rc=run_rc,
            initial_logs=run_logs,
        )
    if run_rc != 0:
        return {
            "model_name": model_name,
            "success": False,
            "stage": "dbt_run",
            "return_code": run_rc,
            "logs": run_logs,
            "target_fallback_used": target_fallback_used,
        }

    test_cmd = ["dbt", "test", "--select", model_name]
    if requested_target and not target_fallback_used:
        test_cmd.extend(["--target", requested_target])
    test_rc, test_out, test_err = _run_command(test_cmd, dbt_project_dir)
    test_logs = (test_err or test_out or "").strip()
    test_rc, test_logs = _retry_on_duckdb_lock(
        dbt_project_dir=dbt_project_dir,
        command=test_cmd,
        initial_rc=test_rc,
        initial_logs=test_logs,
    )
    return {
        "model_name": model_name,
        "success": test_rc == 0,
        "stage": "dbt_test",
        "return_code": test_rc,
        "logs": test_logs,
        "run_return_code": run_rc,
        "run_logs": run_logs,
        "target_fallback_used": target_fallback_used,
    }


def _sanitize_schema_descriptions(*, dbt_project_dir: Path) -> int:
    """
    Repair legacy generated schema.yml description lines that can break YAML parsing.
    """
    models_dir = dbt_project_dir / "models"
    if not models_dir.is_dir():
        return 0
    repaired = 0
    for schema_path in models_dir.rglob("*.schema.yml"):
        try:
            raw = schema_path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = raw.splitlines()
        changed = False
        for idx, line in enumerate(lines):
            m = re.match(r"^(\s*description:\s*)Source column `(.+)`\s*$", line)
            if not m:
                continue
            prefix = m.group(1)
            source = m.group(2)
            lines[idx] = f"{prefix}{json.dumps(f'Source column `{source}`', ensure_ascii=False)}"
            changed = True
        if not changed:
            continue
        try:
            schema_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            repaired += 1
        except OSError:
            continue
    return repaired


def _sanitize_generated_sql_files(*, dbt_project_dir: Path) -> int:
    """
    Remove trailing semicolons from generated model SQL files.

    dbt wraps model SQL in adapter DDL (e.g. create view ... as (...)); an inner trailing
    semicolon can trigger parser errors in adapters like DuckDB.
    """
    models_dir = dbt_project_dir / "models"
    if not models_dir.is_dir():
        return 0
    repaired = 0
    for sql_path in models_dir.rglob("*.sql"):
        try:
            raw = sql_path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Keep file intact if there is no trailing statement terminator.
        trimmed = raw.rstrip()
        if not trimmed.endswith(";"):
            continue
        fixed = trimmed[:-1].rstrip() + "\n"
        if fixed == raw:
            continue
        try:
            sql_path.write_text(fixed, encoding="utf-8")
            repaired += 1
        except OSError:
            continue
    return repaired


def _retry_on_duckdb_lock(
    *,
    dbt_project_dir: Path,
    command: list[str],
    initial_rc: int,
    initial_logs: str,
    retries: int = 3,
) -> tuple[int, str]:
    rc = int(initial_rc)
    logs = str(initial_logs or "")
    for attempt in range(retries):
        if rc == 0 or not _is_duckdb_lock_error(logs):
            return rc, logs
        # Short bounded backoff for transient lock contention.
        time.sleep(0.4 * (attempt + 1))
        rc2, out2, err2 = _run_command(command, dbt_project_dir)
        rc = int(rc2)
        logs = str(err2 or out2 or "")
    return rc, logs


def _is_duckdb_lock_error(logs: str) -> bool:
    msg = str(logs or "").lower()
    return (
        "cannot open file" in msg
        and "duckdb.db" in msg
        and "being used by another process" in msg
    )


def _chunked_models(model_names: list[str], chunk_size: int) -> list[list[str]]:
    size = max(1, int(chunk_size or 1))
    return [model_names[i : i + size] for i in range(0, len(model_names), size)]


def _read_run_results_map(dbt_project_dir: Path) -> dict[str, dict[str, Any]]:
    p = dbt_project_dir / "target" / "run_results.json"
    if not p.is_file():
        return {}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return out
    for row in results:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("unique_id") or "")
        status = str(row.get("status") or "")
        msg = str(row.get("message") or "")
        # unique_id shape: model.<project>.<model_name>
        model_name = uid.rsplit(".", 1)[-1] if "." in uid else uid
        if model_name:
            out[model_name] = {"status": status, "message": msg}
    return out


def test_models_batch(
    *,
    dbt_project_dir: Path,
    model_names: list[str],
    target: str | None = None,
    chunk_size: int = 50,
    report: dict[str, Any] | None = None,
    report_path: Path | None = None,
    model_context: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Validate many models with batched dbt invocations for performance.
    """
    names = [str(n).strip() for n in model_names if str(n).strip()]
    names = list(dict.fromkeys(names))
    if not names:
        return {}
    requested_target = str(target or "").strip()
    meta = model_context or {}
    for mn in names:
        _sanitize_schema_descriptions(dbt_project_dir=dbt_project_dir)
        _sanitize_generated_sql_files(dbt_project_dir=dbt_project_dir)
        ctx = meta.get(mn) or {}
        _ensure_duckdb_sources_for_model(
            dbt_project_dir=dbt_project_dir,
            model_name=mn,
            report=report,
            report_path=report_path,
            primary_table_key=str(ctx.get("table_key") or "").strip() or None,
            schema_yml=str(ctx.get("schema_yml") or "").strip() or None,
        )

    out: dict[str, dict[str, Any]] = {mn: {"success": False, "reason": "not executed"} for mn in names}
    for chunk in _chunked_models(names, chunk_size):
        target_fallback_used = False
        run_cmd = ["dbt", "run", "--select", *chunk]
        if requested_target:
            run_cmd.extend(["--target", requested_target])
        run_rc, run_out, run_err = _run_command(run_cmd, dbt_project_dir)
        run_logs = str(run_err or run_out or "").strip()
        if run_rc != 0 and requested_target and "does not have a target named" in run_logs.lower():
            target_fallback_used = True
            run_cmd = ["dbt", "run", "--select", *chunk]
            run_rc, run_out, run_err = _run_command(run_cmd, dbt_project_dir)
            run_logs = str(run_err or run_out or "").strip()
        run_rc, run_logs = _retry_on_duckdb_lock(
            dbt_project_dir=dbt_project_dir,
            command=run_cmd,
            initial_rc=run_rc,
            initial_logs=run_logs,
        )
        run_map = _read_run_results_map(dbt_project_dir)
        for mn in chunk:
            r = run_map.get(mn) or {}
            ok = str(r.get("status") or "").lower() in {"success", "pass", "ok"}
            msg = str(r.get("message") or run_logs or "").strip()
            out[mn] = {"success": ok, "reason": msg}
        # If run failed, skip tests for failed models in this chunk.
        test_chunk = [mn for mn in chunk if bool(out.get(mn, {}).get("success"))]
        if not test_chunk:
            continue
        test_cmd = ["dbt", "test", "--select", *test_chunk]
        if requested_target and not target_fallback_used:
            test_cmd.extend(["--target", requested_target])
        test_rc, test_out, test_err = _run_command(test_cmd, dbt_project_dir)
        test_logs = str(test_err or test_out or "").strip()
        test_rc, test_logs = _retry_on_duckdb_lock(
            dbt_project_dir=dbt_project_dir,
            command=test_cmd,
            initial_rc=test_rc,
            initial_logs=test_logs,
        )
        test_map = _read_run_results_map(dbt_project_dir)
        # dbt test can return zero results when model has no tests.
        if not test_map:
            for mn in test_chunk:
                out[mn] = {"success": True, "reason": ""}
            continue
        for mn in test_chunk:
            t = test_map.get(mn)
            if not isinstance(t, dict):
                out[mn] = {"success": True, "reason": ""}
                continue
            ok = str(t.get("status") or "").lower() in {"success", "pass", "ok"}
            msg = str(t.get("message") or test_logs or "").strip()
            out[mn] = {"success": ok, "reason": msg}
    return out


def _ensure_duckdb_sources_for_model(
    *,
    dbt_project_dir: Path,
    model_name: str,
    report: dict[str, Any] | None = None,
    report_path: Path | None = None,
    primary_table_key: str | None = None,
    schema_yml: str | None = None,
) -> int:
    """
    Best-effort local bootstrap for missing source schema/table relations in DuckDB.

    When ``report`` / ``report_path`` are provided, source tables are created with real DDL
    column names from the loaded report (not synthetic ``ds_col_*`` placeholders).
    """
    sql_path = _find_model_sql_path(dbt_project_dir=dbt_project_dir, model_name=model_name)
    if sql_path is None:
        return 0
    try:
        sql_text = sql_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    relations = _extract_source_relations(sql_text)
    if not relations:
        return 0
    target_dir = dbt_project_dir / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "duckdb.db"
    try:
        import duckdb  # type: ignore
    except Exception:
        return 0
    created = 0
    try:
        con = duckdb.connect(str(db_path))
    except Exception:
        return 0
    try:
        for schema_name, table_name in relations:
            table_key = f"{schema_name}.{table_name}"
            use_schema_yml = schema_yml if (primary_table_key and table_key == primary_table_key) else None
            columns = _resolve_bootstrap_columns(
                report=report,
                report_path=report_path,
                table_key=table_key,
                schema_yml=use_schema_yml,
                sql_text=sql_text,
            )
            existing = _duckdb_table_columns(con, schema_name, table_name)
            columns = _merge_column_names(columns, existing)
            if not columns:
                columns = [f"ds_col_{i}" for i in range(1, 11)]
            col_def = ", ".join(f'"{c}" VARCHAR' for c in columns)
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
                con.execute(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}"')
                con.execute(f'CREATE TABLE "{schema_name}"."{table_name}" ({col_def})')
                created += 1
            except Exception:
                continue
    finally:
        con.close()
    return created


def _find_model_sql_path(*, dbt_project_dir: Path, model_name: str) -> Path | None:
    from ama.dbt_migration.paths import find_model_sql_path as _find

    return _find(dbt_project_dir=dbt_project_dir, model_name=model_name)


def _extract_source_relations(sql: str) -> list[tuple[str, str]]:
    pairs = re.findall(r"\bfrom\s+([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\b", str(sql or ""), flags=re.IGNORECASE)
    out: list[tuple[str, str]] = []
    for schema_name, table_name in pairs:
        pair = (str(schema_name), str(table_name))
        if pair not in out:
            out.append(pair)
    return out


def _extract_source_columns(sql: str) -> list[str]:
    text = str(sql or "")
    cols = re.findall(r"\b(ds_col_\d+)\b", text, flags=re.IGNORECASE)
    if cols:
        return list(dict.fromkeys(cols))
    # AMA proposals often cast source columns: ``customer_id::VARCHAR AS customer_id``.
    cast_cols = re.findall(r"\b([a-zA-Z_][\w]*)\s*::", text, flags=re.IGNORECASE)
    out: list[str] = []
    skip = {"varchar", "boolean", "timestamp", "integer", "bigint", "double", "date", "text"}
    for c in cast_cols:
        name = str(c)
        if name.lower() in skip:
            continue
        if name not in out:
            out.append(name)
    return out


def apply_fix(
    *,
    dbt_project_dir: Path,
    model_name: str,
    error_log: str,
    attempt_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sql_path = dbt_project_dir / "models" / f"{model_name}.sql"
    failed_sql = ""
    if sql_path.is_file():
        try:
            failed_sql = sql_path.read_text(encoding="utf-8")
        except OSError:
            failed_sql = ""
    corrected_sql, error_analysis, tokens_used, confidence = _run_fix_agent(
        model_name=model_name,
        error_log=error_log,
        failed_sql=failed_sql,
        attempt_history=attempt_history or [],
    )
    return {
        "model_name": model_name,
        "corrected_sql": corrected_sql,
        "error_analysis": error_analysis,
        "tokens_used": int(tokens_used),
        "confidence": float(confidence),
        "success": bool(corrected_sql.strip()),
    }


def commit_to_disk(*, model_name: str, sql: str, schema_yml: str = "") -> dict[str, Any]:
    return {
        "protected": True,
        "pending_write": {
            "model_name": model_name,
            "sql": sql,
            "schema_yml": schema_yml,
        },
    }


def list_waves(*, report: dict[str, Any], model_status_by_name: dict[str, str] | None = None) -> dict[str, Any]:
    return get_report_summary(report=report, model_status_by_name=model_status_by_name)


def analyze_schema(
    *,
    report: dict[str, Any],
    report_path: Path,
    table: str,
    duckdb_path: Path,
    sample_row_cap: int = 10,
) -> dict[str, Any]:
    payload = inspect_table(
        report=report,
        report_path=report_path,
        table_key=table,
        duckdb_path=duckdb_path,
        sample_row_cap=sample_row_cap,
    )
    if (not isinstance(payload.get("sample_rows"), list) or not payload.get("sample_rows")) and (
        payload.get("ddl_columns") or payload.get("observed_columns")
    ):
        cols: list[str] = []
        for c in (payload.get("ddl_columns") or []):
            if isinstance(c, str) and c.strip():
                cols.append(c.strip())
        for c in (payload.get("observed_columns") or []):
            if isinstance(c, str) and c.strip():
                cols.append(c.strip())
        cols = list(dict.fromkeys(cols))
        synthetic = {col: _synthetic_value_for_column(col) for col in cols}
        payload["sample_rows"] = [synthetic]
        payload["sample_rows_source"] = "synthetic_from_ddl"
    # Always provide inferred types so UI can display meaningful column types.
    inferred_types: dict[str, str] = {}
    for c in (payload.get("ddl_columns") or []):
        if isinstance(c, str) and c.strip():
            inferred_types[c.strip()] = _infer_column_type(c)
    for c in (payload.get("observed_columns") or []):
        if isinstance(c, str) and c.strip():
            inferred_types[c.strip()] = _infer_column_type(c)
    payload["inferred_types"] = inferred_types

    if payload.get("sample_rows_warning"):
        payload["note"] = "DuckDB sample rows unavailable; returning DDL columns only."

    # If the table includes Hebrew-named columns, attach deterministic transliteration
    # mappings (no OpenAI call) so the UI can render a verification table.
    import re
    from ama.dbt_migration.mapping import build_mapping_row
    from ama.dbt_migration.models import MappingSource

    all_cols: list[str] = []
    for c in (payload.get("ddl_columns") or []):
        all_cols.append(str(c))
    for c in (payload.get("observed_columns") or []):
        all_cols.append(str(c))
    all_cols = list(dict.fromkeys(all_cols))

    hebrew_columns: list[dict[str, Any]] = []
    heb_pat = re.compile(r"[\u0590-\u05FF]")
    glossary: dict[str, str] = {}
    alias_registry: dict[str, str] = {}
    for col in all_cols:
        if not heb_pat.search(col or ""):
            continue
        mapping = build_mapping_row(col, glossary=glossary, alias_registry=alias_registry)
        hebrew_columns.append(
            {
                "hebrew_name": mapping.hebrew_name,
                "english_alias": mapping.english_alias,
                "source": mapping.source.value,
            }
        )
    if hebrew_columns:
        payload["hebrew_columns"] = hebrew_columns
    return payload


def generate_synthetic_rows(
    *,
    report: dict[str, Any],
    report_path: Path,
    table: str,
    duckdb_path: Path,
    row_count: int = 10,
    sample_row_cap: int = 10,
) -> dict[str, Any]:
    """
    Generate mock rows for the given table.

    Implementation detail:
    - Reuses `inspect_table` logic so we get either real DuckDB samples or synthetic fallback.
    - Returns a structured payload that the router can summarize in `final`.
    """
    table_key = str(table).strip()
    cap = max(1, int(row_count or sample_row_cap or 10))
    # Reuse analyze_schema so we benefit from existing synthetic fallback behavior
    # when DuckDB rows are unavailable.
    payload = analyze_schema(
        report=report,
        report_path=report_path,
        table=table_key,
        duckdb_path=duckdb_path,
        sample_row_cap=cap,
    )
    rows = payload.get("sample_rows") if isinstance(payload.get("sample_rows"), list) else []
    if not rows:
        # Last-resort deterministic fallback: build rows from known columns.
        cols: list[str] = []
        for c in (payload.get("ddl_columns") or []):
            if isinstance(c, str) and c.strip():
                cols.append(c.strip())
        for c in (payload.get("observed_columns") or []):
            if isinstance(c, str) and c.strip():
                cols.append(c.strip())
        cols = list(dict.fromkeys(cols))
        if cols:
            rows = [{col: _synthetic_value_for_column(col) for col in cols} for _ in range(cap)]
            payload["sample_rows"] = rows
            payload["sample_rows_source"] = "synthetic_last_resort"
    # Keep response small: only expose what is needed for QA summaries.
    out: dict[str, Any] = {
        "table_key": table_key,
        "row_cap": cap,
        "sample_rows": rows,
        "sample_rows_available": bool(payload.get("sample_rows_available")),
        "sample_rows_warning": payload.get("sample_rows_warning") if isinstance(payload.get("sample_rows_warning"), str) else "",
        "inferred_types": payload.get("inferred_types") if isinstance(payload.get("inferred_types"), dict) else {},
        "sample_rows_source": str(payload.get("sample_rows_source") or ""),
    }
    return out


def validate_sql_on_duckdb(
    *,
    sql: str,
    dialect: str = "duckdb",
    schema_provider=None,  # NEW: optional live explain
) -> dict[str, Any]:
    """
    Fast syntax validation using SQLGlot (sqlglot-backed, not an actual execution).

    The function name is kept aligned with router intent: it is intended to prevent
    dbt test failures caused by invalid SQL syntax before any write/test gate.
    """
    target = validate_target_dialect(dialect)
    ok, reasons = validate_sql_with_sqlglot(sql, target_dialect=target)

    explain_result = {"ok": True, "plan": "skipped", "error": None, "dialect": "static"}
    if schema_provider is not None:
        try:
            er = schema_provider.execute_explain(sql)
            explain_result = {
                "ok": er.ok,
                "plan": er.plan,
                "error": er.error,
                "dialect": er.dialect,
            }
            if not er.ok and er.error:
                ok = False
                reasons.append(f"Live DB EXPLAIN failed: {er.error}")
        except Exception as exc:
            reasons.append(f"Live DB EXPLAIN error: {exc}")

    return {
        "ok": ok,
        "dialect": target.value,
        "reasons": reasons,
        "explain_plan": explain_result.get("plan", ""),
        "explain_dialect": explain_result.get("dialect", ""),
        "explain_error": explain_result.get("error"),
    }


def list_live_tables(schema_filter: str | None = None) -> dict[str, Any]:
    """
    Live discovery helper for MCP SchemaProvider.

    Uses AMA_SCHEMA_MODE / AMA_DB_CONNECTION_STRING from the environment.
    Always returns a safe structure; never raises.
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider()
        tables = provider.list_tables(schema_filter=schema_filter)
        return {"tables": tables, "count": len(tables)}
    except Exception:
        return {"tables": [], "count": 0}


def explain_sql_live(sql: str) -> dict[str, Any]:
    """
    Live EXPLAIN helper for MCP SchemaProvider.

    Uses AMA_SCHEMA_MODE / AMA_DB_CONNECTION_STRING from the environment.
    Always returns a safe structure; never raises.
    """
    from ama.mcp.factory import get_schema_provider

    try:
        provider = get_schema_provider()
        er = provider.execute_explain(sql)
        return {"ok": er.ok, "plan": er.plan, "error": er.error, "dialect": er.dialect}
    except Exception as exc:
        return {"ok": False, "plan": "", "error": str(exc), "dialect": ""}


def propose_dbt_model(
    *,
    report: dict[str, Any],
    report_path: Path,
    table: str,
    dialect: str,
    glossary_path: Path | None,
) -> dict[str, Any]:
    return generate_sql(
        report=report,
        report_path=report_path,
        table_key=table,
        dialect=dialect,
        glossary_path=glossary_path,
    )


def execute_dbt_test(*, dbt_project_dir: Path, model: str) -> dict[str, Any]:
    return test_model(dbt_project_dir=dbt_project_dir, model_name=model)


def request_write_permission(
    *,
    model: str,
    sql: str,
    schema_yml: str = "",
    mapping_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = commit_to_disk(model_name=model, sql=sql, schema_yml=schema_yml)
    pending = payload.get("pending_write") or {}
    pending["mapping_rows"] = mapping_rows or []
    payload["pending_write"] = pending
    return payload


@dataclass
class QueryInventoryResult:
    tables: list[dict[str, Any]]
    total: int
    filters: dict[str, Any]
    sort_by: str


@dataclass
class BulkMigrateResult:
    migrated: list[str]
    skipped: list[dict[str, str]]
    dry_run: bool
    contract: MigrationContract | None


@dataclass
class ExplainResult:
    table_key: str
    queue: str
    confidence: ConfidenceResult
    criticality: CriticalityResult
    anomaly_flags: list[AnomalyFlag]
    summary: str


def query_inventory(
    *,
    report: dict[str, Any],
    filters: dict[str, Any] | None = None,
    sort_by: str = "confidence_score",
    sort_order: str = "desc",
    limit: int | None = None,
) -> QueryInventoryResult:
    filt = dict(filters or {})
    eval_result = evaluate_batch(report=report, dry_run=True)
    rows_out: list[dict[str, Any]] = []
    for s in eval_result.scored_tables:
        if filt.get("queue") and str(filt["queue"]).lower() != s.queue:
            continue
        if filt.get("domain") and str(filt["domain"]).lower() != s.business_domain.lower():
            continue
        if "confidence_min" in filt and s.confidence < int(filt["confidence_min"]):
            continue
        if "confidence_max" in filt and s.confidence > int(filt["confidence_max"]):
            continue
        if "criticality_min" in filt and s.criticality < int(filt["criticality_min"]):
            continue
        if "criticality_max" in filt and s.criticality > int(filt["criticality_max"]):
            continue
        if filt.get("anomaly_level"):
            level = str(filt["anomaly_level"]).upper()
            if not any(f.level == level for f in s.anomaly_flags):
                continue
        if filt.get("schema"):
            parts = s.table_key.split(".")
            schema_part = parts[0] if len(parts) >= 2 else ""
            if str(filt["schema"]).lower() != schema_part.lower():
                continue
        if filt.get("status"):
            inv_row = next(
                (r for r in _extract_inventory(report) if str(r.get("full_name")) == s.table_key),
                {},
            )
            if str(inv_row.get("status") or "") != str(filt["status"]):
                continue
        if filt.get("has_blob") is True:
            if not any(f.name == "unsupported_blob_type" for f in s.anomaly_flags):
                continue
        rows_out.append(
            {
                "table_key": s.table_key,
                "queue": s.queue,
                "confidence": s.confidence,
                "criticality": s.criticality,
                "anomaly_flags": [f"{f.level}:{f.name}" for f in s.anomaly_flags],
                "business_domain": s.business_domain,
                "reason": s.confidence_result.reason,
            }
        )

    reverse = str(sort_order).lower() != "asc"
    key = "confidence"
    if sort_by in {"criticality", "criticality_score"}:
        key = "criticality"
    elif sort_by in {"table_key", "business_domain"}:
        key = sort_by

    def _sort_key(r: dict[str, Any]) -> Any:
        return r.get(key)

    rows_out.sort(key=_sort_key, reverse=reverse)
    if isinstance(limit, int) and limit > 0:
        rows_out = rows_out[:limit]
    return QueryInventoryResult(tables=rows_out, total=len(rows_out), filters=filt, sort_by=sort_by)


def bulk_migrate_tables(
    *,
    report: dict[str, Any],
    report_path: Path,
    filters: dict[str, Any],
    dialect: str,
    glossary_path: Path | None,
    dry_run: bool = True,
    approved_by: str = "agent",
) -> BulkMigrateResult:
    if str(filters.get("queue") or "green").lower() == "red":
        return BulkMigrateResult(
            migrated=[],
            skipped=[{"table_key": "*", "queue": "red", "reason": "bulk migration of RED queue is not allowed"}],
            dry_run=dry_run,
            contract=None,
        )
    q = query_inventory(report=report, filters=filters)
    eval_result = evaluate_batch(report=report, dry_run=True)
    score_by_table = {s.table_key: s for s in eval_result.scored_tables}
    if dry_run:
        for s in eval_result.scored_tables:
            if s.queue == "green" and any(str(r.get("table_key")) == s.table_key for r in q.tables):
                decision_from_queue(s.queue)
        return BulkMigrateResult(migrated=[], skipped=[], dry_run=True, contract=eval_result.contract_preview)

    migrated: list[str] = []
    skipped: list[dict[str, str]] = []
    for row in q.tables:
        table_key = str(row.get("table_key") or "")
        scored = score_by_table.get(table_key)
        if scored is None:
            continue
        if scored.queue != "green":
            skipped.append({"table_key": table_key, "queue": scored.queue, "reason": "not in GREEN queue"})
            continue
        proposal = propose_dbt_model(
            report=report,
            report_path=report_path,
            table=table_key,
            dialect=dialect,
            glossary_path=glossary_path,
        )
        model_name = str(proposal.get("model_name") or table_key.replace(".", "_"))
        sql = str(proposal.get("sql") or "")
        request_write_permission(
            model=model_name,
            sql=sql,
            schema_yml=str(proposal.get("schema_yml") or ""),
        )
        append_decision(
            table_key=table_key,
            decision=decision_from_queue(scored.queue),
            confidence=scored.confidence_result,
            criticality=scored.criticality_result,
            anomaly_flags=scored.anomaly_flags,
            contract_id=eval_result.contract_preview.contract_id,
            approved_by=approved_by,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        migrated.append(table_key)
    return BulkMigrateResult(
        migrated=migrated,
        skipped=skipped,
        dry_run=False,
        contract=eval_result.contract_preview,
    )


def explain_table_score(
    *,
    report: dict[str, Any],
    table_key: str,
    conf_floor: int | None = None,
    crit_ceil: int | None = None,
) -> ExplainResult:
    from ama.scale_engine import DEFAULT_CONF_FLOOR, DEFAULT_CRIT_CEIL

    floor = DEFAULT_CONF_FLOOR if conf_floor is None else conf_floor
    ceil = DEFAULT_CRIT_CEIL if crit_ceil is None else crit_ceil
    eval_result = evaluate_batch(report=report, dry_run=True, conf_floor=floor, crit_ceil=ceil)
    scored = next((s for s in eval_result.scored_tables if s.table_key == table_key), None)
    if scored is None:
        empty_conf = ConfidenceResult(score=0, reason="table not found in inventory", components={})
        empty_crit = CriticalityResult(score=0, reason="table not found in inventory", components={})
        return ExplainResult(
            table_key=table_key,
            queue="red",
            confidence=empty_conf,
            criticality=empty_crit,
            anomaly_flags=[
                AnomalyFlag(level="BLOCK", name="missing_table", reason="table not found in inventory")
            ],
            summary=f"{table_key} is missing from inventory and cannot be scored.",
        )
    summary = (
        f"{table_key} is in {scored.queue.upper()} queue with confidence {scored.confidence} and "
        f"criticality {scored.criticality}. Confidence reason: {scored.confidence_result.reason}. "
        f"Criticality reason: {scored.criticality_result.reason}."
    )
    return ExplainResult(
        table_key=table_key,
        queue=scored.queue,
        confidence=scored.confidence_result,
        criticality=scored.criticality_result,
        anomaly_flags=scored.anomaly_flags,
        summary=summary,
    )
