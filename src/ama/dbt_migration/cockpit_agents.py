from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from ama.ai_query_helper import OpenAIAuthError, OpenAIQueryError, query_openai_json
from ama.dbt_migration.agent_prompts import get_agent_prompt
from ama.env_resolver import has_openai_api_key


def _llm_enabled() -> bool:
    return has_openai_api_key()


_PLACEHOLDER_RE = re.compile(r"^(?:sample|fallback)_(\d+)$")


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return _PLACEHOLDER_RE.match(value) is not None


def _fmt_money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _iso_utc(day_offset: int) -> str:
    base = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    d = base + dt.timedelta(days=day_offset)
    # Use Z to keep JSON compact and consistent.
    return d.isoformat().replace("+00:00", "Z")


def _find_first_col(schema_columns: list[str], predicate) -> str | None:
    for c in schema_columns:
        if predicate(c):
            return c
    return None


def _make_realistic_row(schema_columns: list[str], row_index: int) -> dict[str, Any]:
    """
    Type-aware synthetic values based on column-name patterns.

    This is deterministic and used both for fallback mode and as a sanitizer
    when the LLM returns placeholder-like tokens (e.g. sample_0).
    """
    row: dict[str, Any] = {}

    amount_col = _find_first_col(schema_columns, lambda c: c.lower() == "amount" or "amount" in c.lower() and "vat" not in c.lower())
    net_amount_col = _find_first_col(schema_columns, lambda c: c.lower() == "net_amount" or "net_amount" in c.lower() or c.lower() == "net" or ("net" in c.lower() and "amount" in c.lower()))
    vat_rate_col = _find_first_col(schema_columns, lambda c: "vat" in c.lower() and "rate" in c.lower())

    if amount_col or net_amount_col or vat_rate_col:
        vat_rate_options = [Decimal("0"), Decimal("5"), Decimal("8"), Decimal("17")]
        vat_rate = vat_rate_options[row_index % len(vat_rate_options)]
        net = Decimal("1000") + (Decimal(row_index) * Decimal("123.45"))
        amount = net * (Decimal("1") + (vat_rate / Decimal("100")))
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount_col:
            row[amount_col] = _fmt_money(amount)
        if net_amount_col:
            row[net_amount_col] = _fmt_money(net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if vat_rate_col:
            # Keep as numeric string for UI.
            row[vat_rate_col] = _fmt_money(vat_rate)

    status_col = _find_first_col(schema_columns, lambda c: "status" in c.lower())
    if status_col:
        statuses = ["OPEN", "PAID", "VOID", "CANCELLED", "SENT", "RECEIVED", "COMPLETED"]
        row[status_col] = statuses[row_index % len(statuses)]

    for col in schema_columns:
        col_l = col.lower()
        if col in row:
            continue  # Already generated (e.g. monetary block above).
        if col_l.endswith("_id") or col_l == "id" or col_l.endswith("id"):
            # Stable-but-human-ish identifiers.
            if "invoice" in col_l:
                row[col] = f"INV-{100000 + row_index}"
            elif "order" in col_l:
                row[col] = f"ORD-{200000 + row_index}"
            else:
                row[col] = str(1000 + row_index)
        elif col_l.endswith("_at") or "created" in col_l or "updated" in col_l or "date" in col_l:
            # ISO-8601 date/time; always UTC so the UI/tests are stable.
            row[col] = _iso_utc(row_index * 7)
        elif "email" in col_l:
            row[col] = f"user{row_index}@example.com"
        elif "vat" in col_l:
            row[col] = _fmt_money(Decimal("17"))  # Reasonable default if we didn't compute it above.
        elif "description" in col_l or "notes" in col_l or "comment" in col_l:
            filler = "x" * 220
            row[col] = f"{col}_{row_index}_{filler}"
        else:
            # Generic strings that still look structured.
            row[col] = f"{col}_{row_index}"

    return row


def _sanitize_complex_mock_data(
    rows: list[dict[str, Any]],
    schema_columns: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raw_row = {}
        # Generate a fully realistic row, then only override placeholder-like values.
        realistic_row = _make_realistic_row(schema_columns, row_index=i)
        sanitized: dict[str, Any] = dict(raw_row)
        for col in schema_columns:
            if col not in sanitized or _is_placeholder(sanitized.get(col)):
                if col in realistic_row:
                    sanitized[col] = realistic_row[col]
        out.append(sanitized)
    return out


def _fallback(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return payload, {"tokens_used": 0, "confidence": 0.0, "is_fallback_active": True}


def wave_summary_agent(wave_id: str, model_names: list[str], model_states: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _llm_enabled():
        return _fallback(
            {
                "health": "stable" if all(model_states.get(m) == "SUCCESS" for m in model_names) else "needs_attention",
                "structural_risks": ["Dependency and data-quality drift checks recommended."],
                "confidence_aggregation": 0.7,
            }
        )
    user = json.dumps({"wave_id": wave_id, "model_names": model_names, "model_states": model_states})
    try:
        res = query_openai_json(
            system_prompt=get_agent_prompt("wave_summary_agent"),
            user_prompt=user,
            max_tokens=900,
            timeout_seconds=40,
            temperature=0.1,
        )
        p = res.payload if isinstance(res.payload, dict) else {}
        return p, {"tokens_used": res.tokens_used, "confidence": float(p.get("confidence_aggregation") or 0.0), "is_fallback_active": False}
    except OpenAIAuthError:
        raise
    except OpenAIQueryError as exc:
        msg = str(exc).strip()
        msg_short = (msg[:180] + "...") if len(msg) > 180 else msg
        return _fallback(
            {
                "health": "fallback",
                "structural_risks": [f"LLM fallback active: {msg_short or 'query error'}."],
                "confidence_aggregation": 0.0,
            }
        )


def risk_agent(sql: str, model_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _llm_enabled():
        return _fallback({"risk_level": "Medium", "concerns": ["Review joins and null handling.", "Validate projected columns against schema drift."]})
    user = json.dumps({"model_name": model_name, "sql": sql})
    try:
        res = query_openai_json(
            system_prompt=get_agent_prompt("risk_agent"),
            user_prompt=user,
            max_tokens=900,
            timeout_seconds=35,
            temperature=0.0,
        )
        p = res.payload if isinstance(res.payload, dict) else {}
        return p, {"tokens_used": res.tokens_used, "confidence": float(p.get("confidence") or 0.0), "is_fallback_active": False}
    except OpenAIAuthError:
        raise
    except OpenAIQueryError as exc:
        msg = str(exc).strip()
        msg_short = (msg[:180] + "...") if len(msg) > 180 else msg
        return _fallback({"risk_level": "Medium", "concerns": [f"Fallback risk scan active ({msg_short or 'query error'})."]})


def scenario_agent(sql: str, model_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _llm_enabled():
        return _fallback({"scenarios": [f"What if primary key duplicates appear in {model_name}?", "What if timestamp values are future-dated?", "What if nullable business columns are blank?"]})
    user = json.dumps({"model_name": model_name, "sql": sql})
    try:
        res = query_openai_json(
            system_prompt=get_agent_prompt("scenario_agent"),
            user_prompt=user,
            max_tokens=900,
            timeout_seconds=35,
            temperature=0.1,
        )
        p = res.payload if isinstance(res.payload, dict) else {}
        return p, {"tokens_used": res.tokens_used, "confidence": float(p.get("confidence") or 0.0), "is_fallback_active": False}
    except OpenAIAuthError:
        raise
    except OpenAIQueryError as exc:
        msg = str(exc).strip()
        msg_short = (msg[:180] + "...") if len(msg) > 180 else msg
        return _fallback(
            {
                "scenarios": [
                    "Fallback scenario: duplicate key stress test.",
                    "Fallback scenario: null-heavy records.",
                    "Fallback scenario: schema drift column removal.",
                    f"LLM fallback reason: {msg_short or 'query error'}.",
                ]
            }
        )


def data_gen_agent(model_name: str, schema_columns: list[str], row_count: int = 10) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _llm_enabled():
        limited = min(row_count, 5)
        rows = [_make_realistic_row(schema_columns[:8], row_index=i) for i in range(limited)]
        return _fallback({"complex_mock_data": rows, "confidence": 0.0})
    user = json.dumps({"model_name": model_name, "schema_columns": schema_columns, "row_count": row_count})
    try:
        res = query_openai_json(
            system_prompt=get_agent_prompt("data_gen_agent"),
            user_prompt=user,
            max_tokens=1200,
            timeout_seconds=45,
            temperature=0.2,
        )
        p = res.payload if isinstance(res.payload, dict) else {}
        rows = p.get("complex_mock_data")
        if isinstance(rows, list):
            sanitized_rows = _sanitize_complex_mock_data(
                [r for r in rows if isinstance(r, dict)],
                schema_columns=schema_columns,
            )
            p["complex_mock_data"] = sanitized_rows
        else:
            # If the LLM returned an unexpected shape, fall back to deterministic realistic rows.
            limited = min(row_count, 5)
            p["complex_mock_data"] = [_make_realistic_row(schema_columns[:8], row_index=i) for i in range(limited)]
        confidence = float(p.get("confidence") or 0.0)
        return p, {"tokens_used": res.tokens_used, "confidence": confidence, "is_fallback_active": False}
    except OpenAIAuthError:
        raise
    except OpenAIQueryError as exc:
        msg = str(exc).strip()
        msg_short = (msg[:180] + "...") if len(msg) > 180 else msg
        limited = min(row_count, 3)
        rows = [_make_realistic_row(schema_columns[:8], row_index=i) for i in range(limited)]
        return _fallback({"complex_mock_data": rows, "confidence": 0.0, "fallback_reason": msg_short or "query_error"})


def chat_model_agent(model_name: str, sql: str, question: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _llm_enabled():
        return _fallback({"answer": "Fallback mode active; manual SQL review recommended.", "sql_patch_proposal": "-- No patch proposal in fallback mode"})
    user = json.dumps({"model_name": model_name, "sql": sql, "question": question})
    try:
        res = query_openai_json(
            system_prompt=get_agent_prompt("chat_model_agent"),
            user_prompt=user,
            max_tokens=1100,
            timeout_seconds=45,
            temperature=0.1,
        )
        p = res.payload if isinstance(res.payload, dict) else {}
        return p, {"tokens_used": res.tokens_used, "confidence": float(p.get("confidence") or 0.0), "is_fallback_active": False}
    except OpenAIAuthError:
        raise
    except OpenAIQueryError as exc:
        msg = str(exc).strip()
        msg_short = (msg[:180] + "...") if len(msg) > 180 else msg
        return _fallback({"answer": f"LLM unavailable ({msg_short or 'query error'}). Patch suggestions are temporarily disabled.", "sql_patch_proposal": "-- fallback"})
