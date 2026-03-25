"""
Stage 2: LLM-based translation for RTL tokens not resolved by co-occurrence.

Uses the same OpenAI integration pattern as ama.business_logic._openai_enrich.
Requires AMA_OPENAI_API_KEY or OPENAI_API_KEY environment variable.
No-ops silently if the key is absent.
"""

from __future__ import annotations

import json
from typing import Any

from ama.env_resolver import get_openai_api_key, get_openai_model


def translate_rtl_tokens(
    unresolved_tokens: list[str],
    ddl_columns: list[str],
    *,
    model: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Ask an LLM to translate unresolved RTL (Hebrew/Arabic) SQL column tokens
    to their most likely English DDL column equivalents.

    Returns: { rtl_token -> {"target_column": str, "confidence": float, "explanation": str} }
    Returns empty dict if no API key is set or if the call fails.

    Batches up to 80 tokens per call to stay within token limits.
    """
    api_key = get_openai_api_key()
    if not api_key:
        return {}
    if not unresolved_tokens or not ddl_columns:
        return {}

    _model = model or get_openai_model("gpt-4o-mini")
    results: dict[str, dict[str, Any]] = {}

    # Batch to avoid token limit
    BATCH = 80
    for i in range(0, len(unresolved_tokens), BATCH):
        batch = unresolved_tokens[i : i + BATCH]
        results.update(_call_llm(batch, ddl_columns, api_key, _model))

    return results


def _call_llm(
    tokens: list[str],
    ddl_columns: list[str],
    api_key: str,
    model: str,
) -> dict[str, dict[str, Any]]:
    """Single LLM batch call. Returns empty dict on any error."""
    try:
        import urllib.request

        ddl_str = ", ".join(f'"{c}"' for c in ddl_columns[:150])
        tokens_str = "\n".join(f"- {t}" for t in tokens)

        prompt = f"""You are a data migration specialist. The following are Hebrew (or Arabic) database column names found in legacy SQL logs. Map each one to its most likely English equivalent from the DDL column list provided. Return ONLY a valid JSON object with no markdown, no explanation outside the JSON.

DDL columns available: [{ddl_str}]

Hebrew/RTL column names to translate:
{tokens_str}

Return JSON format exactly:
{{
  "translations": [
    {{
      "source_term": "<hebrew term>",
      "target_column": "<best DDL column or null if no match>",
      "confidence": <0.0-1.0>,
      "explanation": "<one short sentence>"
    }}
  ]
}}

Rules:
- target_column must be from the DDL list or null
- confidence 0.9+ = certain, 0.7-0.9 = likely, 0.5-0.7 = plausible, <0.5 = uncertain
- If no DDL column is a reasonable match, set target_column to null and confidence to 0.0"""

        payload = json.dumps(
            {
                "model": model,
                "max_tokens": 1500,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        raw = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())

        out: dict[str, dict[str, Any]] = {}
        for entry in parsed.get("translations", []):
            if not isinstance(entry, dict):
                continue
            src = str(entry.get("source_term") or "").strip()
            tgt = entry.get("target_column")
            conf = float(entry.get("confidence") or 0.0)
            expl = str(entry.get("explanation") or "").strip()
            if src and tgt and isinstance(tgt, str) and conf > 0:
                out[src] = {
                    "target_column": tgt.strip(),
                    "confidence": min(1.0, max(0.0, conf)),
                    "explanation": expl,
                }
        return out

    except Exception:
        return {}
