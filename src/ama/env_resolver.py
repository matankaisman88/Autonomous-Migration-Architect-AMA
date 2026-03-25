from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and ((value[0] == value[-1]) and value[0] in {"'", '"'}):
            value = value[1:-1]
        out[key] = value
    return out


def _dotenv_values() -> dict[str, str]:
    cwd_env = Path.cwd() / ".env"
    repo_env = _repo_root() / ".env"
    merged: dict[str, str] = {}
    # CWD has higher precedence for interactive runs.
    merged.update(_parse_dotenv(repo_env))
    if cwd_env != repo_env:
        merged.update(_parse_dotenv(cwd_env))
    return merged


def get_env(name: str, default: str = "") -> str:
    direct = os.environ.get(name)
    if direct is not None and str(direct).strip():
        return str(direct).strip()
    dot = _dotenv_values().get(name)
    if dot is not None and str(dot).strip():
        return str(dot).strip()
    return default


def get_openai_api_key() -> str:
    return get_env("AMA_OPENAI_API_KEY") or get_env("OPENAI_API_KEY")


def has_openai_api_key() -> bool:
    return bool(get_openai_api_key())


def get_openai_model(default: str = "gpt-4o-mini") -> str:
    return get_env("AMA_OPENAI_MODEL", default)

