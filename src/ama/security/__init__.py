"""
Secure handling of paths and secrets for AMA.

Credentials must come from environment variables or ``.env`` (via ``pydantic-settings``),
never from committed files. Use :func:`redact_path` in logs and user-facing errors.
"""

from ama.security.credentials import (
    redact_path,
    safe_path_repr,
)

__all__ = [
    "redact_path",
    "safe_path_repr",
]
