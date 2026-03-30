from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ama.ai_query_helper import (
    OpenAIAuthError,
    OpenAIInvalidResponseError,
    OpenAIQueryError,
    OpenAIRateLimitError,
    query_openai_json,
)
from ama.dbt_migration.agent_prompts import get_agent_prompt
from ama.env_resolver import has_openai_api_key, get_openai_model
from ama.migration_agent import agent_tools


_DEFAULT_TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "list_waves", "args": {}},
    {"name": "analyze_schema", "args": {"table": "string"}},
    {"name": "propose_dbt_model", "args": {"table": "string", "dialect": "duckdb|snowflake|bigquery|redshift"}},
    {"name": "execute_dbt_test", "args": {"model": "string"}},
    {"name": "apply_fix", "args": {"model": "string", "error_log": "string"}},
    {"name": "request_write_permission", "args": {"model": "string", "sql": "string", "schema_yml": "optional string"}},
    {"name": "generate_synthetic_rows", "args": {"table": "string", "row_count": "optional int"}},
    {"name": "validate_sql_on_duckdb", "args": {"sql": "string", "dialect": "optional duckdb|snowflake|bigquery|redshift"}},
    {"name": "query_inventory", "args": {"filters": "optional dict", "sort_by": "optional string", "limit": "optional int"}},
    {
        "name": "bulk_migrate_tables",
        "args": {"filters": "dict", "dialect": "duckdb|snowflake|bigquery|redshift", "dry_run": "optional bool (default true)"},
    },
    {"name": "explain_table_score", "args": {"table_key": "string"}},
]


@dataclass
class AgentTurnResult:
    state: dict[str, Any]
    status: str
    message: str = ""
    pending_write: dict[str, Any] | None = None
    tokens_used: int = 0
    cost_est: float = 0.0


def _estimate_cost(tokens_used: int) -> float:
    pricing = {
        "gpt-4o-mini": {"input_per_1k": 0.00015, "output_per_1k": 0.0006},
        "default": {"input_per_1k": 0.0002, "output_per_1k": 0.0008},
    }
    model_name = str(get_openai_model("default"))
    rates = pricing.get(model_name, pricing["default"])
    return ((tokens_used / 2) / 1000.0) * rates["input_per_1k"] + ((tokens_used / 2) / 1000.0) * rates["output_per_1k"]


def init_state(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault(
        "messages",
        [
            {
                "role": "system",
                "content": (
                    "You are the AMA Migration Assistant. You help users migrate SQL from legacy SQL Server "
                    "reports to dbt models. You MUST use the `request_write_permission` tool before finalizing "
                    "any file creation. You have access to the AMA report which contains wave structure, table "
                    "inventory, and business rationale."
                ),
            }
        ],
    )
    state.setdefault("pending_write", None)
    state.setdefault("tokens_used_total", 0)
    state.setdefault("cost_est_total", 0.0)
    state.setdefault("model_status_by_name", {})
    return state


def _system_prompt() -> str:
    base = get_agent_prompt("migration_agent_router")
    return (
        "You are a Senior Data Engineer specializing in dbt and AMA.\n"
        + base
    )


def _tool_prompt_context() -> str:
    tools = [t.get("function", {}) for t in agent_tools.get_tools() if isinstance(t, dict)]
    return json.dumps(
        {
            "contract": {
                "allowed_responses": [
                    {"tool_request": {"name": "tool_name", "args": {}}},
                    {"final": {"message": "string"}},
                ],
                "rules": [
                    "Return valid JSON only.",
                    "Do not include markdown.",
                    "Use tools for side effects and data reads.",
                    "Use commit_to_disk only when SQL is ready for approval.",
                ],
            },
            "tools": tools or _DEFAULT_TOOL_SPECS,
        },
        ensure_ascii=False,
    )


def _append_message(state: dict[str, Any], *, role: str, content: str) -> None:
    state["messages"].append({"role": role, "content": content})


def _llm_payload_from_state(state: dict[str, Any]) -> str:
    hist = list(state.get("messages", []))
    if len(hist) > 15:
        system_msgs = [m for m in hist if isinstance(m, dict) and m.get("role") == "system"][:1]
        tail = [m for m in hist if not (isinstance(m, dict) and m.get("role") == "system")][-14:]
        hist = system_msgs + tail
        state["messages"] = hist
    return json.dumps({"toolbox": _tool_prompt_context(), "history": hist}, ensure_ascii=False)


def _dispatch_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    report: dict[str, Any],
    report_path: Path,
    dbt_project_dir: Path,
    glossary_path: Path | None,
    output_dir: Path,
    sample_row_cap: int,
    default_dialect: str,
    on_protected_commit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if tool_name in {"list_waves", "get_report_summary"}:
        return agent_tools.list_waves(
            report=report,
            model_status_by_name=args.get("model_status_by_name") if isinstance(args.get("model_status_by_name"), dict) else None,
        )
    if tool_name in {"analyze_schema", "inspect_table"}:
        table_key = str(args.get("table") or args.get("table_key") or "").strip()
        return agent_tools.analyze_schema(
            report=report,
            report_path=report_path,
            table=table_key,
            duckdb_path=dbt_project_dir / "target" / "duckdb.db",
            sample_row_cap=sample_row_cap,
        )
    if tool_name in {"propose_dbt_model", "generate_sql"}:
        table_key = str(args.get("table") or args.get("table_key") or "").strip()
        requested = str(args.get("dialect") or default_dialect or "duckdb").strip().lower()
        allowed = {"duckdb", "snowflake", "bigquery", "redshift"}
        dialect = requested if requested in allowed else str(default_dialect or "duckdb")
        return agent_tools.propose_dbt_model(
            report=report,
            report_path=report_path,
            table=table_key,
            dialect=dialect,
            glossary_path=glossary_path,
        )
    if tool_name in {"execute_dbt_test", "test_model"}:
        model_name = str(args.get("model") or args.get("model_name") or "").strip()
        return agent_tools.execute_dbt_test(dbt_project_dir=dbt_project_dir, model=model_name)
    if tool_name == "apply_fix":
        model_name = str(args.get("model") or args.get("model_name") or "").strip()
        error_log = str(args.get("error_log") or "")
        attempt_history = args.get("attempt_history")
        if not isinstance(attempt_history, list):
            attempt_history = []
        return agent_tools.apply_fix(
            dbt_project_dir=dbt_project_dir,
            model_name=model_name,
            error_log=error_log,
            attempt_history=attempt_history,
        )
    if tool_name in {"request_write_permission", "commit_to_disk"}:
        model_name = str(args.get("model") or args.get("model_name") or "").strip()
        sql = str(args.get("sql") or "")
        schema_yml = str(args.get("schema_yml") or "")
        mapping_rows = args.get("mapping_rows")
        payload = agent_tools.request_write_permission(
            model=model_name,
            sql=sql,
            schema_yml=schema_yml,
            mapping_rows=mapping_rows if isinstance(mapping_rows, list) else None,
        )
        if on_protected_commit is not None:
            on_protected_commit(payload.get("pending_write") or {})
        return payload
    if tool_name == "generate_synthetic_rows":
        table_key = str(args.get("table") or args.get("table_key") or "").strip()
        row_count = args.get("row_count")
        try:
            rc_int = int(row_count) if row_count is not None else None
        except (TypeError, ValueError):
            rc_int = None
        if rc_int is None:
            rc_int = sample_row_cap
        duckdb_path = dbt_project_dir / "target" / "duckdb.db"
        return agent_tools.generate_synthetic_rows(
            report=report,
            report_path=report_path,
            table=table_key,
            duckdb_path=duckdb_path,
            row_count=rc_int,
            sample_row_cap=sample_row_cap,
        )
    if tool_name == "validate_sql_on_duckdb":
        sql = str(args.get("sql") or "")
        dialect = str(args.get("dialect") or default_dialect or "duckdb").strip().lower()
        return agent_tools.validate_sql_on_duckdb(sql=sql, dialect=dialect)
    if tool_name == "list_live_tables":
        schema_filter = args.get("schema_filter")
        return agent_tools.list_live_tables(
            schema_filter=str(schema_filter).strip() if schema_filter is not None else None
        )
    if tool_name == "explain_sql_live":
        sql = str(args.get("sql") or "")
        return agent_tools.explain_sql_live(sql=sql)
    if tool_name == "query_inventory":
        lim_raw = args.get("limit")
        try:
            lim_int = int(lim_raw) if lim_raw is not None else None
        except (TypeError, ValueError):
            lim_int = None
        res = agent_tools.query_inventory(
            report=report,
            filters=args.get("filters") if isinstance(args.get("filters"), dict) else None,
            sort_by=str(args.get("sort_by") or "confidence_score"),
            sort_order=str(args.get("sort_order") or "desc"),
            limit=lim_int,
        )
        return {"tables": res.tables, "total": res.total, "filters": res.filters, "sort_by": res.sort_by}
    if tool_name == "bulk_migrate_tables":
        res = agent_tools.bulk_migrate_tables(
            report=report,
            report_path=report_path,
            filters=args.get("filters") if isinstance(args.get("filters"), dict) else {},
            dialect=str(args.get("dialect") or default_dialect or "duckdb"),
            glossary_path=glossary_path,
            dry_run=bool(args.get("dry_run", True)),
            approved_by="agent",
        )
        contract = None
        if res.contract is not None:
            contract = {
                "rules": res.contract.rules,
                "contract_id": res.contract.contract_id,
                "table_count": res.contract.table_count,
                "excluded": res.contract.excluded,
            }
        return {"migrated": res.migrated, "skipped": res.skipped, "dry_run": res.dry_run, "contract": contract}
    if tool_name == "explain_table_score":
        table_key = str(args.get("table_key") or args.get("table") or "").strip()
        res = agent_tools.explain_table_score(report=report, table_key=table_key)
        return {
            "table_key": res.table_key,
            "queue": res.queue,
            "confidence": {
                "score": res.confidence.score,
                "reason": res.confidence.reason,
                "components": res.confidence.components,
            },
            "criticality": {
                "score": res.criticality.score,
                "reason": res.criticality.reason,
                "components": res.criticality.components,
            },
            "anomaly_flags": [{"level": f.level, "name": f.name, "reason": f.reason} for f in res.anomaly_flags],
            "summary": res.summary,
        }
    raise ValueError(f"unknown tool: {tool_name}")


def run_agent_turn(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    report_path: Path,
    dbt_project_dir: Path,
    output_dir: Path,
    glossary_path: Path | None,
    user_message: str | None = None,
    tool_result_message: dict[str, Any] | None = None,
    sample_row_cap: int = 10,
    max_steps: int = 12,
    on_tool_start: Callable[[str], None] | None = None,
    default_dialect: str = "duckdb",
) -> AgentTurnResult:
    init_state(state)
    if user_message:
        _append_message(state, role="user", content=user_message)
    if tool_result_message is not None:
        # Insert a synthetic tool message so the UI can render structured tool output
        # and the LLM can see the tool outcome.
        tool_name = None
        if isinstance(tool_result_message, dict):
            tool_name = tool_result_message.get("tool_name")
        tool_result = None
        if isinstance(tool_result_message, dict):
            tool_result = tool_result_message.get("result") or tool_result_message.get("test_result") or tool_result_message
        state["messages"].append(
            {
                "role": "tool",
                "content": json.dumps(tool_result_message, ensure_ascii=False),
                "tool_name": str(tool_name or "tool"),
                "tool_result": tool_result,
            }
        )

    if not has_openai_api_key():
        msg = "AMA_OPENAI_API_KEY is missing. Configure it to use Migration Agent."
        _append_message(state, role="assistant", content=msg)
        return AgentTurnResult(state=state, status="error", message=msg)

    used_tokens = 0
    for _ in range(max_steps):
        try:
            # Retry once for malformed model payloads to improve UX stability.
            try:
                result = query_openai_json(
                    system_prompt=_system_prompt(),
                    user_prompt=_llm_payload_from_state(state),
                    max_tokens=1400,
                    timeout_seconds=45,
                    model=get_openai_model("gpt-4o-mini"),
                    temperature=0.0,
                )
            except OpenAIInvalidResponseError:
                result = query_openai_json(
                    system_prompt=_system_prompt() + "\nReturn strict JSON only.",
                    user_prompt=_llm_payload_from_state(state),
                    max_tokens=1400,
                    timeout_seconds=45,
                    model=get_openai_model("gpt-4o-mini"),
                    temperature=0.0,
                )
        except (OpenAIAuthError, OpenAIRateLimitError, OpenAIQueryError) as exc:
            msg = f"Migration Agent error: {exc}"
            _append_message(state, role="assistant", content=msg)
            return AgentTurnResult(state=state, status="error", message=msg)

        used_tokens += int(result.tokens_used or 0)
        payload = result.payload if isinstance(result.payload, dict) else {}
        tool_req = payload.get("tool_request")
        final = payload.get("final")

        if isinstance(final, dict):
            message = str(final.get("message") or "").strip() or "Done."
            _append_message(state, role="assistant", content=message)
            state["tokens_used_total"] = int(state.get("tokens_used_total", 0)) + used_tokens
            cost_est = _estimate_cost(used_tokens)
            state["cost_est_total"] = float(state.get("cost_est_total", 0.0)) + cost_est
            return AgentTurnResult(
                state=state,
                status="final",
                message=message,
                tokens_used=used_tokens,
                cost_est=cost_est,
            )

        if not isinstance(tool_req, dict):
            tool_error = {"error": "Invalid LLM response. Expected tool_request/final JSON object."}
            _append_message(state, role="tool", content=json.dumps(tool_error, ensure_ascii=False))
            continue

        name = str(tool_req.get("name") or "").strip()
        args = tool_req.get("args")
        if not isinstance(args, dict):
            args = {}

        pending_write: dict[str, Any] = {}
        try:
            if on_tool_start is not None:
                on_tool_start(name)
            tool_out = _dispatch_tool(
                tool_name=name,
                args=args,
                report=report,
                report_path=report_path,
                dbt_project_dir=dbt_project_dir,
                glossary_path=glossary_path,
                output_dir=output_dir,
                sample_row_cap=sample_row_cap,
                default_dialect=default_dialect,
                on_protected_commit=lambda p: pending_write.update(p),
            )
        except Exception as exc:
            tool_out = {"tool_name": name, "error": str(exc)}

        _append_message(
            state,
            role="tool",
            content=json.dumps({"tool_name": name, "result": tool_out}, ensure_ascii=False),
        )
        state["messages"][-1]["tool_name"] = name
        state["messages"][-1]["tool_result"] = tool_out
        if name in {"request_write_permission", "commit_to_disk"} and pending_write:
            state["pending_write"] = pending_write
            state["tokens_used_total"] = int(state.get("tokens_used_total", 0)) + used_tokens
            cost_est = _estimate_cost(used_tokens)
            state["cost_est_total"] = float(state.get("cost_est_total", 0.0)) + cost_est
            return AgentTurnResult(
                state=state,
                status="pending_write",
                pending_write=pending_write,
                tokens_used=used_tokens,
                cost_est=cost_est,
            )

    message = "Agent reached max tool steps without final response."
    _append_message(state, role="assistant", content=message)
    state["tokens_used_total"] = int(state.get("tokens_used_total", 0)) + used_tokens
    cost_est = _estimate_cost(used_tokens)
    state["cost_est_total"] = float(state.get("cost_est_total", 0.0)) + cost_est
    return AgentTurnResult(
        state=state,
        status="max_steps",
        message=message,
        tokens_used=used_tokens,
        cost_est=cost_est,
    )
