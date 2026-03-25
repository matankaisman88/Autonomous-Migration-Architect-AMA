from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_prompts() -> dict[str, str]:
    path = Path(__file__).with_name("prompts.yaml")
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("prompts.yaml must contain an object")
    out: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def get_agent_prompt(prompt_key: str) -> str:
    prompts = _load_prompts()
    value = prompts.get(prompt_key)
    if not value:
        raise ValueError(f"missing prompt key: {prompt_key}")
    return value
