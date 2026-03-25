from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ama.dbt_migration.generator import generate_model_artifact
from ama.dbt_migration.models import TargetDialect
from ama.dbt_migration.sql_self_heal import validate_sql_with_sqlglot
from ama.dbt_migration.runner import _run_command, _run_fix_agent
from ama.dbt_migration.sql_transpile import validate_target_dialect
from ama.planner import AutonomousPlanner


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
    if not isinstance(importance, list):
        return out
    for row in importance:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_table") or "").strip() != table_key:
            continue
        col = str(row.get("column") or "").strip()
        if col:
            out.append(col)
    return list(dict.fromkeys(out))


def _load_manifest_table_columns(report: dict[str, Any], report_path: Path) -> dict[str, list[str]]:
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


def test_model(*, dbt_project_dir: Path, model_name: str) -> dict[str, Any]:
    # Important: `dbt test` can return success when a model has zero tests.
    # Run `dbt run` first so invalid SQL is caught before test-only pass states.
    run_rc, run_out, run_err = _run_command(["dbt", "run", "--select", model_name], dbt_project_dir)
    run_logs = (run_err or run_out or "").strip()
    if run_rc != 0:
        return {
            "model_name": model_name,
            "success": False,
            "stage": "dbt_run",
            "return_code": run_rc,
            "logs": run_logs,
        }

    test_rc, test_out, test_err = _run_command(["dbt", "test", "--select", model_name], dbt_project_dir)
    test_logs = (test_err or test_out or "").strip()
    return {
        "model_name": model_name,
        "success": test_rc == 0,
        "stage": "dbt_test",
        "return_code": test_rc,
        "logs": test_logs,
        "run_return_code": run_rc,
        "run_logs": run_logs,
    }


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


def validate_sql_on_duckdb(*, sql: str, dialect: str = "duckdb") -> dict[str, Any]:
    """
    Fast syntax validation using SQLGlot (sqlglot-backed, not an actual execution).

    The function name is kept aligned with router intent: it is intended to prevent
    dbt test failures caused by invalid SQL syntax before any write/test gate.
    """
    target = validate_target_dialect(dialect)
    ok, reasons = validate_sql_with_sqlglot(sql, target_dialect=target)
    return {
        "ok": ok,
        "dialect": target.value,
        "reasons": reasons,
    }


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
