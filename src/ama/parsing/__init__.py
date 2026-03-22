"""SQL parse backends for AMA ingestion."""

from ama.parsing.backend import (
    DIALECT_ALIASES,
    ParseBackend,
    ParseResult,
    SqlGlotParseBackend,
    default_parse_backend,
    normalize_dialect,
)
from ama.parsing.sqlglot_extract import extract_from_select, qualified_key_from_table

__all__ = [
    "DIALECT_ALIASES",
    "ParseBackend",
    "ParseResult",
    "SqlGlotParseBackend",
    "default_parse_backend",
    "extract_from_select",
    "normalize_dialect",
    "qualified_key_from_table",
]
