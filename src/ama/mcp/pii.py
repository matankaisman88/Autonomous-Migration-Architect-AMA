"""
PII Anonymizer — masks sensitive values in sample data BEFORE sending to LLM.

Patterns masked:
  - Email addresses        → user_***@***.***
  - Israeli phone numbers  → ***-***-****
  - International phones   → ***-***-****
  - Israeli ID (9 digits)  → [ID MASKED]
  - Credit card numbers    → **** **** **** ****
  - Full names (heuristic) → [NAME MASKED]  (only when column name hints at name)

Design rules:
  1. Never raises. If masking fails for a value, return the original unchanged.
  2. All regex patterns are compiled once at module import (not per-call).
  3. No external dependencies — stdlib only.
  4. Masking is deterministic (same input → same output) for auditability.
"""
from __future__ import annotations

import re
from typing import Any

# ── compiled patterns ──────────────────────────────────────────────────────────
_EMAIL    = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)
_PHONE_IL = re.compile(r"\b0[0-9]{1,2}[-\s]?[0-9]{3}[-\s]?[0-9]{4}\b")
_PHONE_INT= re.compile(r"\+?[1-9][0-9]{1,3}[-\s]?[0-9]{3,4}[-\s]?[0-9]{4,6}")
_ID_IL    = re.compile(r"\b[0-9]{9}\b")
_CC       = re.compile(r"\b(?:[0-9]{4}[-\s]?){3}[0-9]{4}\b")

# Column name hints for full-name detection
_NAME_COLS = re.compile(
    r"\b(name|שם|full_name|שם_מלא|first|last|fname|lname|customer_name|שם_לקוח)\b",
    re.I,
)

# Simple heuristic: 2–4 Hebrew/Latin words that look like a person's name
_NAME_VAL = re.compile(r"^[\u0590-\u05FFa-zA-Z]{2,20}(\s[\u0590-\u05FFa-zA-Z]{2,20}){1,3}$")


def _mask_value(column_name: str, value: Any) -> Any:
    """Mask a single cell value. Returns original type when not a string."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if not v:
        return value

    # email — highest priority
    if _EMAIL.search(v):
        return _EMAIL.sub(lambda m: _mask_email(m.group()), v)

    # credit card
    if _CC.search(v):
        return _CC.sub("**** **** **** ****", v)

    # Israeli ID (9 digits standalone)
    if _ID_IL.fullmatch(v):
        return "[ID MASKED]"

    # phone (Israeli then international)
    if _PHONE_IL.search(v):
        return _PHONE_IL.sub("***-***-****", v)
    if _PHONE_INT.search(v):
        return _PHONE_INT.sub("***-***-****", v)

    # full name (column-hint + value heuristic)
    if _NAME_COLS.search(column_name) and _NAME_VAL.match(v):
        return "[NAME MASKED]"

    return value


def _mask_email(email: str) -> str:
    at = email.find("@")
    if at < 0:
        return "***@***.***"
    local = email[:at]
    domain = email[at + 1:]
    masked_local = local[:1] + "***" if len(local) > 1 else "***"
    parts = domain.split(".")
    masked_domain = "***" + "." + parts[-1] if parts else "***"
    return f"{masked_local}@{masked_domain}"


def mask_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Return a new dict with PII-masked values.
    Column names are preserved; only values are masked.
    """
    try:
        return {col: _mask_value(col, val) for col, val in row.items()}
    except Exception:
        # Safety net — never crash the API over PII masking
        return dict(row)


def mask_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [mask_row(r) for r in rows]

