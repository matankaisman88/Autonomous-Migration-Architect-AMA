from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from ama.env_resolver import get_openai_api_key, get_openai_model


class OpenAIQueryError(Exception):
    pass


class OpenAIAuthError(OpenAIQueryError):
    pass


class OpenAIRateLimitError(OpenAIQueryError):
    pass


class OpenAITimeoutError(OpenAIQueryError):
    pass


class OpenAIInvalidResponseError(OpenAIQueryError):
    pass


@dataclass(frozen=True)
class AIQueryResult:
    payload: dict[str, Any]
    tokens_used: int


def resolve_openai_api_key() -> str:
    api_key = get_openai_api_key()
    if not api_key:
        raise OpenAIAuthError("AMA_OPENAI_API_KEY or OPENAI_API_KEY is required")
    return api_key


def query_openai_json(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    timeout_seconds: int,
    model: str | None = None,
    temperature: float = 0.0,
) -> AIQueryResult:
    api_key = resolve_openai_api_key()
    model_name = model or get_openai_model("gpt-4o-mini")
    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            raise OpenAIAuthError(f"OpenAI auth failed with status {exc.code}") from exc
        if exc.code == 429:
            raise OpenAIRateLimitError("OpenAI rate limited (429)") from exc
        raise OpenAIQueryError(f"OpenAI HTTP error {exc.code}") from exc
    except TimeoutError as exc:
        raise OpenAITimeoutError("OpenAI request timed out") from exc
    except error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower():
            raise OpenAITimeoutError("OpenAI request timed out") from exc
        raise OpenAIQueryError(f"OpenAI request failed: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise OpenAIInvalidResponseError("OpenAI response is not valid JSON") from exc

    try:
        content = str(raw["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenAIInvalidResponseError("OpenAI response missing message content") from exc
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        # Some models include leading/trailing commentary before the JSON.
        # Attempt to extract the first JSON object/array and parse that.
        extracted = None
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, flags=re.IGNORECASE)
        if fence:
            extracted = fence.group(1).strip()
        else:
            first_obj = content.find("{")
            last_obj = content.rfind("}")
            if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
                extracted = content[first_obj : last_obj + 1].strip()
            else:
                first_arr = content.find("[")
                last_arr = content.rfind("]")
                if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
                    extracted = content[first_arr : last_arr + 1].strip()
        if extracted:
            try:
                payload = json.loads(extracted)
            except json.JSONDecodeError:
                raise OpenAIInvalidResponseError("OpenAI message content is not valid JSON") from exc
        else:
            raise OpenAIInvalidResponseError("OpenAI message content is not valid JSON") from exc
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    tokens_used = int(usage.get("total_tokens") or 0)
    return AIQueryResult(payload=payload, tokens_used=tokens_used)
