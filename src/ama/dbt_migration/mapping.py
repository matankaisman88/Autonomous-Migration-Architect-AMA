from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from ama.ai_query_helper import (
    OpenAIAuthError,
    OpenAIQueryError,
    OpenAIRateLimitError,
    query_openai_json,
)
from ama.dbt_migration.agent_prompts import get_agent_prompt
from ama.dbt_migration.models import MappingRow, MappingSource
from ama.env_resolver import has_openai_api_key

logger = logging.getLogger(__name__)

_HEBREW_CHAR_MAP = {
    "א": "a",
    "ב": "b",
    "ג": "g",
    "ד": "d",
    "ה": "h",
    "ו": "v",
    "ז": "z",
    "ח": "kh",
    "ט": "t",
    "י": "y",
    "כ": "k",
    "ך": "k",
    "ל": "l",
    "מ": "m",
    "ם": "m",
    "נ": "n",
    "ן": "n",
    "ס": "s",
    "ע": "a",
    "פ": "p",
    "ף": "p",
    "צ": "ts",
    "ץ": "ts",
    "ק": "q",
    "ר": "r",
    "ש": "sh",
    "ת": "t",
}


def _contains_hebrew(value: str) -> bool:
    return bool(re.search(r"[\u0590-\u05FF]", value or ""))


def transliterate_to_snake_case(value: str) -> str:
    buf: list[str] = []
    for ch in unicodedata.normalize("NFKC", value or ""):
        if ch in _HEBREW_CHAR_MAP:
            buf.append(_HEBREW_CHAR_MAP[ch])
        elif ch.isascii() and (ch.isalnum() or ch == "_"):
            buf.append(ch.lower())
        elif ch in {" ", "-", ".", "/"}:
            buf.append("_")
        elif ch.isdigit():
            buf.append(ch)
        else:
            buf.append("_")
    normalized = re.sub(r"_+", "_", "".join(buf)).strip("_")
    return normalized or "col"


def build_mapping_row(
    raw_name: str,
    glossary: dict[str, str],
    alias_registry: dict[str, str],
) -> MappingRow:
    glossary_confidence = 1.0
    translit_confidence = 0.55
    key = (raw_name or "").strip()
    has_hebrew = _contains_hebrew(key)
    if key in alias_registry:
        alias = alias_registry[key]
        if key in glossary:
            source = MappingSource.GLOSSARY
            warnings: list[str] = []
            conf = glossary_confidence
        else:
            # If the key is already an ASCII identifier (no Hebrew codepoints),
            # treat it as a stable name rather than “low-quality transliteration”.
            source = MappingSource.TRANSLITERATION
            if has_hebrew:
                warnings = ["[TRANSLITERATION_WARNING]"]
                conf = translit_confidence
            else:
                warnings = []
                conf = glossary_confidence
        return MappingRow(
            hebrew_name=key,
            english_alias=alias,
            source=source,
            confidence=conf,
            warning_flags=warnings,
        )
    if key in glossary and glossary[key].strip():
        alias = transliterate_to_snake_case(glossary[key].strip())
        alias_registry[key] = alias
        return MappingRow(
            hebrew_name=key,
            english_alias=alias,
            source=MappingSource.GLOSSARY,
            confidence=glossary_confidence,
            warning_flags=[],
        )
    alias = transliterate_to_snake_case(key)
    alias_registry[key] = alias
    if has_hebrew:
        return MappingRow(
            hebrew_name=key,
            english_alias=alias,
            source=MappingSource.TRANSLITERATION,
            confidence=translit_confidence,
            warning_flags=["[TRANSLITERATION_WARNING]"],
        )
    return MappingRow(
        hebrew_name=key,
        english_alias=alias,
        source=MappingSource.TRANSLITERATION,
        confidence=glossary_confidence,
        warning_flags=[],
    )


def _build_translation_prompt(
    *,
    unresolved_columns: list[str],
    glossary: dict[str, str],
    model_business_logic: str,
) -> str:
    return json.dumps(
        {
            "columns": unresolved_columns,
            "glossary": glossary,
            "model_business_logic": model_business_logic,
            "response_schema": {
                "mappings": [
                    {"old_name": "string", "new_name": "string", "confidence": "float_0_to_1"}
                ],
                "confidence": "float_0_to_1",
            },
        },
        ensure_ascii=False,
    )


def apply_semantic_translation_for_unresolved(
    *,
    mapped_rows: list[MappingRow],
    glossary: dict[str, str],
    model_business_logic: str,
    alias_registry: dict[str, str],
) -> tuple[list[MappingRow], dict[str, Any]]:
    unresolved = [
        row.hebrew_name
        for row in mapped_rows
        if row.source == MappingSource.TRANSLITERATION and _contains_hebrew(row.hebrew_name)
    ]
    if not unresolved:
        return mapped_rows, {
            "tokens_used": 0,
            "confidence": 1.0,
            "is_fallback_active": False,
            "auth_error": False,
            "rate_limit_error": False,
        }
    if not has_openai_api_key():
        return mapped_rows, {
            "tokens_used": 0,
            "confidence": 0.0,
            "is_fallback_active": True,
            "auth_error": False,
            "rate_limit_error": False,
        }

    system_prompt = get_agent_prompt("translation_agent")
    user_prompt = _build_translation_prompt(
        unresolved_columns=unresolved,
        glossary=glossary,
        model_business_logic=model_business_logic,
    )
    try:
        result = query_openai_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=1200,
            timeout_seconds=30,
            temperature=0.0,
        )
    except OpenAIAuthError:
        raise
    except OpenAIRateLimitError:
        logger.warning(
            "llm_agent_fallback",
            extra={
                "agent_name": "translation_agent",
                "tokens_used": 0,
                "confidence": 0.0,
                "is_fallback_active": True,
            },
        )
        return mapped_rows, {
            "tokens_used": 0,
            "confidence": 0.0,
            "is_fallback_active": True,
            "auth_error": False,
            "rate_limit_error": True,
        }
    except OpenAIQueryError:
        logger.warning(
            "llm_agent_fallback",
            extra={
                "agent_name": "translation_agent",
                "tokens_used": 0,
                "confidence": 0.0,
                "is_fallback_active": True,
            },
        )
        return mapped_rows, {
            "tokens_used": 0,
            "confidence": 0.0,
            "is_fallback_active": True,
            "auth_error": False,
            "rate_limit_error": False,
        }

    payload = result.payload
    entries = payload.get("mappings")
    if not isinstance(entries, list):
        logger.warning(
            "llm_agent_fallback",
            extra={
                "agent_name": "translation_agent",
                "tokens_used": result.tokens_used,
                "confidence": 0.0,
                "is_fallback_active": True,
            },
        )
        return mapped_rows, {
            "tokens_used": result.tokens_used,
            "confidence": 0.0,
            "is_fallback_active": True,
            "auth_error": False,
            "rate_limit_error": False,
        }

    map_by_source: dict[str, dict[str, Any]] = {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        source = str(row.get("old_name") or "").strip()
        new_name = str(row.get("new_name") or "").strip()
        try:
            conf = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if source and new_name:
            map_by_source[source] = {"new_name": transliterate_to_snake_case(new_name), "confidence": conf}

    try:
        batch_confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        batch_confidence = 0.0

    out_rows: list[MappingRow] = []
    for row in mapped_rows:
        replacement = map_by_source.get(row.hebrew_name)
        if replacement is None:
            out_rows.append(row)
            continue
        alias = str(replacement["new_name"])
        alias_registry[row.hebrew_name] = alias
        out_rows.append(
            MappingRow(
                hebrew_name=row.hebrew_name,
                english_alias=alias,
                source=MappingSource.GLOSSARY,
                confidence=float(replacement.get("confidence") or 0.0),
                warning_flags=[],
            )
        )
    logger.info(
        "llm_agent_telemetry",
        extra={
            "agent_name": "translation_agent",
            "tokens_used": result.tokens_used,
            "confidence": round(batch_confidence, 4),
            "is_fallback_active": False,
        },
    )
    return out_rows, {
        "tokens_used": result.tokens_used,
        "confidence": batch_confidence,
        "is_fallback_active": False,
        "auth_error": False,
        "rate_limit_error": False,
    }
