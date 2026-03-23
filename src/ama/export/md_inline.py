"""
Convert a small subset of Markdown in rationale text for export sinks:

- ``**text**`` → HTML ``<strong>`` or ADF ``strong`` mark
- `` `text` `` → HTML ``<code>`` or ADF ``code`` mark

Other characters are escaped for HTML (plain segments) or passed through as ADF text.
"""

from __future__ import annotations

import html
from typing import Any


def iter_md_segments(s: str) -> list[tuple[str, str]]:
    """Split *s* into (kind, segment) where kind is ``plain``, ``bold``, or ``code``."""
    parts: list[tuple[str, str]] = []
    i = 0
    while i < len(s):
        if s[i] == "`":
            j = s.find("`", i + 1)
            if j == -1:
                parts.append(("plain", s[i]))
                i += 1
                continue
            parts.append(("code", s[i + 1 : j]))
            i = j + 1
        elif s.startswith("**", i):
            j = s.find("**", i + 2)
            if j == -1:
                parts.append(("plain", s[i]))
                i += 1
                continue
            parts.append(("bold", s[i + 2 : j]))
            i = j + 2
        else:
            nxt = len(s)
            ni = s.find("`", i)
            nb = s.find("**", i)
            if ni >= 0:
                nxt = min(nxt, ni)
            if nb >= 0:
                nxt = min(nxt, nb)
            parts.append(("plain", s[i:nxt]))
            i = nxt
    return parts


def md_inline_to_html(s: str) -> str:
    """Turn ``**`` / `` ` `` spans into HTML; escape plain segments."""
    chunks: list[str] = []
    for kind, seg in iter_md_segments(s):
        if kind == "plain":
            chunks.append(html.escape(seg))
        elif kind == "bold":
            chunks.append(f"<strong>{html.escape(seg)}</strong>")
        else:
            chunks.append(f"<code>{html.escape(seg)}</code>")
    return "".join(chunks)


def _merge_adjacent_adf_text(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive ADF text nodes when marks match."""
    if not nodes:
        return [{"type": "text", "text": ""}]
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not out:
            out.append(n)
            continue
        prev = out[-1]
        if (
            prev.get("type") == "text"
            and n.get("type") == "text"
            and prev.get("marks") == n.get("marks")
        ):
            prev["text"] = str(prev.get("text", "")) + str(n.get("text", ""))
        else:
            out.append(n)
    return out


def md_inline_to_adf_paragraph_content(s: str) -> list[dict[str, Any]]:
    """ADF ``paragraph.content`` list for one line of *s* (no newlines)."""
    nodes: list[dict[str, Any]] = []
    for kind, seg in iter_md_segments(s):
        if kind == "plain":
            nodes.append({"type": "text", "text": seg})
        elif kind == "bold":
            nodes.append({"type": "text", "text": seg, "marks": [{"type": "strong"}]})
        else:
            nodes.append({"type": "text", "text": seg, "marks": [{"type": "code"}]})
    return _merge_adjacent_adf_text(nodes)


def adf_document_from_markdown(text: str) -> dict[str, Any]:
    """
    Build a Jira ADF document: one ``paragraph`` per line (``\\n``), each with
    inline ** / `` ` `` converted to strong / code marks.
    """
    lines = text.split("\n")
    content: list[dict[str, Any]] = []
    for line in lines:
        para_content = md_inline_to_adf_paragraph_content(line)
        content.append({"type": "paragraph", "content": para_content})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"type": "doc", "version": 1, "content": content}
