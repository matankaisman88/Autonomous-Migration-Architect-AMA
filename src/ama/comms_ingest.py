from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ama.sanitize import sanitize_text


@dataclass
class CommsChunk:
    text: str
    source: str
    channel: str | None
    ts: str | None
    meta: dict[str, Any]


def _slack_export_messages(export_dir: Path) -> Iterator[CommsChunk]:
    """Read Slack export layout: channel folders with JSONL day files."""
    for channel_dir in sorted(export_dir.iterdir()):
        if not channel_dir.is_dir():
            continue
        channel = channel_dir.name
        for jpath in sorted(channel_dir.glob("*.json")):
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, list):
                continue
            for msg in data:
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text") or ""
                if not isinstance(text, str) or not text.strip():
                    continue
                text = sanitize_text(text.strip())
                uid = msg.get("user") or msg.get("username") or ""
                ts = str(msg.get("ts") or "")
                yield CommsChunk(
                    text=text,
                    source="slack_export",
                    channel=channel,
                    ts=ts,
                    meta={"user": uid, "file": str(jpath)},
                )


def _simple_jsonl(path: Path) -> Iterator[CommsChunk]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = rec.get("text") or rec.get("body") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            text = sanitize_text(text.strip())
            yield CommsChunk(
                text=text,
                source=str(path),
                channel=rec.get("channel"),
                ts=str(rec.get("ts") or rec.get("timestamp") or ""),
                meta={k: v for k, v in rec.items() if k not in ("text", "body")},
            )


def iter_comms(comms_dir: Path) -> Iterator[CommsChunk]:
    """
    Discover Slack-style export dirs or flat JSONL files under comms_dir.
    """
    if not comms_dir.exists():
        return
    # Single JSONL file
    if comms_dir.is_file() and comms_dir.suffix.lower() == ".jsonl":
        yield from _simple_jsonl(comms_dir)
        return

    for jsonl in sorted(comms_dir.glob("*.jsonl")):
        yield from _simple_jsonl(jsonl)

    # Slack export: subdirs with json arrays
    subdirs = [p for p in comms_dir.iterdir() if p.is_dir()]
    if subdirs:
        for sd in subdirs:
            yield from _slack_export_messages(sd)


def mention_score(text: str, table: str, schema: str | None = None) -> float:
    """
    Lightweight keyword score: full table name, schema.table, and bare table token.
    """
    t = table.lower()
    blob = text.lower()
    score = 0.0
    if schema:
        st = f"{schema.lower()}.{t}"
        score += 3.0 * blob.count(st)
    score += 2.0 * blob.count(t)
    # word boundary-ish
    if re.search(rf"\b{re.escape(t)}\b", blob):
        score += 1.0
    return score


def aggregate_comms_for_table(
    comms_dir: Path,
    *,
    schema: str,
    table: str,
) -> tuple[float, int]:
    """Returns (total mention score, chunk count with any hit)."""
    total = 0.0
    hits = 0
    for chunk in iter_comms(comms_dir):
        s = mention_score(chunk.text, table, schema)
        if s > 0:
            hits += 1
            total += s
    return total, hits
