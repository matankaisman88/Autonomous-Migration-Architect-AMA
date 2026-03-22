"""
Utilities for logging and displaying paths without leaking home directories or tokens.

AMA does not store API keys in code; ``AMA_OPENAI_API_KEY`` and similar are read via
``IngestionSettings`` / environment only.
"""

from __future__ import annotations

import re
from pathlib import Path


def redact_path(path: str | Path, *, keep_segments: int = 2) -> str:
    """
    Return a shortened, user-safe path string for logs (last ``keep_segments`` parts only).

    Example: ``C:\\Users\\alice\\proj\\data\\log.json`` → ``...\\data\\log.json``
    """
    p = Path(path)
    try:
        parts = p.parts
    except (TypeError, ValueError):
        return "<invalid path>"
    if len(parts) <= keep_segments:
        return str(p)
    tail = Path(*parts[-keep_segments:])
    return f".../{tail.as_posix()}"


def safe_path_repr(path: str | Path) -> str:
    """Path suitable for exception messages (no expansion of secrets; relative when possible)."""
    try:
        p = Path(path).resolve()
        cwd = Path.cwd()
        try:
            rel = p.relative_to(cwd)
            return str(rel)
        except ValueError:
            return redact_path(p, keep_segments=3)
    except OSError:
        return redact_path(path, keep_segments=2)


def mask_secret(value: str | None, *, visible_tail: int = 4) -> str:
    """
    Mask a token or password for display (e.g. ``****abcd``).

    Empty or short values return a fixed placeholder.
    """
    if not value:
        return "<unset>"
    v = str(value).strip()
    if len(v) <= visible_tail:
        return "****"
    return "****" + v[-visible_tail:]


# Patterns that should never appear in committed code or logs verbatim
_SECRET_LINE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*['\"]?[^\s'\"]+",
)


def looks_like_secret_line(line: str) -> bool:
    """Heuristic: True if a line resembles ``KEY=value`` for secrets (for CI linting helpers)."""
    return bool(_SECRET_LINE_PATTERN.search(line))


def expand_user_safe(path: Path) -> Path:
    """
    Resolve ``~`` using the current OS user only; prefer passing explicit paths from settings.
    """
    return path.expanduser().resolve()


def ensure_under_root(path: Path, root: Path) -> Path:
    """
    Ensure ``path`` resolves under ``root`` (prevents path traversal when joining user input).

    Raises ``ValueError`` if the resolved path escapes ``root``.
    """
    rp = path.resolve()
    rr = root.resolve()
    try:
        rp.relative_to(rr)
    except ValueError as e:
        raise ValueError(f"path must be under {rr}: {safe_path_repr(rp)}") from e
    return rp


def default_data_root() -> Path:
    """Preferred project root for relative AMA paths (``AMA_*`` paths are often relative to this)."""
    from ama.config import project_root

    return project_root()
