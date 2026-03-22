from __future__ import annotations

import re
import unicodedata


# Control characters to strip (except common whitespace used in SQL strings)
_C0_REMOVE = set(chr(i) for i in range(32)) - {"\t", "\n", "\r"}


def sanitize_text(s: str) -> str:
    """
    Remove null bytes, most C0 control characters, and normalize Unicode (NFC).
    Safe for vector embeddings and LLM prompts.
    """
    if not s:
        return ""
    s = s.replace("\x00", "")
    out: list[str] = []
    for ch in s:
        if ch in _C0_REMOVE:
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch not in "\t\n\r":
            continue
        out.append(ch)
    s2 = "".join(out)
    return unicodedata.normalize("NFC", s2)


def sanitize_sql_text(sql: str) -> str:
    """Pre-parse SQL: strip dangerous bytes and normalize Unicode."""
    return sanitize_text(sql).strip()


_WS_RE = re.compile(r"\s+")


def normalize_sql_identifier(name: str) -> str:
    """
    Normalize a single identifier for comparison / stats keys.
    - NFC, strip quotes/brackets
    - ASCII-only identifiers: casefold for stable Latin keys
    - Non-ASCII (e.g. Hebrew): preserve normalized form (casefold is a no-op)
    """
    s = sanitize_text(name)
    s = s.strip('"').strip("`").strip("[").strip("]").strip()
    s = _WS_RE.sub("_", s.strip())
    if not s:
        return ""
    if all(ord(c) < 128 for c in s):
        return s.casefold()
    return unicodedata.normalize("NFC", s)


# Hebrew, Arabic, and related scripts that need RTL isolation in LTR terminals / JSON viewers
_RTL_SCRIPT_RE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0700-\u074F]")


def has_rtl_script(s: str) -> bool:
    return bool(s and _RTL_SCRIPT_RE.search(s))


# Identifiers that are safe to mirror as a whole (no Latin letters — Hebrew/Arabic + _ + digits)
_RTL_ONLY_IDENTIFIER = re.compile(r"^[\u0590-\u05FF\u0600-\u06FF_0-9]+$")


def mirror_rtl_identifier_for_ltr_console(s: str) -> str:
    """
    Many LTR consoles (e.g. Windows PowerShell) print logical RTL text in reversed visual order.
    For RTL-only identifiers, mirror code-unit order so the line reads correctly left-to-right.
    Mixed Latin/Hebrew names are left unchanged.
    """
    if not s or not _RTL_ONLY_IDENTIFIER.match(s):
        return s
    return s[::-1]


def embed_rtl_isolate_for_display(s: str) -> str:
    """
    Legacy: Unicode RLI/PDI embed (often invisible; some fonts show marks).
    Prefer mirror_rtl_identifier_for_ltr_console for Windows LTR terminals.
    """
    if not s or not has_rtl_script(s):
        return s
    return "\u2067" + s + "\u2069"


def strip_bidi_embedding(s: str) -> str:
    """Remove common directional isolate marks from a display-wrapped identifier."""
    for mark in ("\u2067", "\u2069", "\u2066", "\u2068", "\u200f", "\u200e"):
        s = s.replace(mark, "")
    return s


def is_generic_low_signal_name(name: str) -> bool:
    """
    Heuristic for junk / placeholder columns (Tier-3 style).
    Down-weights merge confidence — does not imply semantic uselessness alone.
    """
    n = normalize_sql_identifier(name)
    if not n:
        return True
    if re.fullmatch(r"flag_\d+", n, re.IGNORECASE):
        return True
    if re.fullmatch(r"temp_0*\d+", n, re.IGNORECASE):
        return True
    if re.fullmatch(r"col\d+", n, re.IGNORECASE):
        return True
    if re.fullmatch(r"x_\d+", n, re.IGNORECASE):
        return True
    return False
