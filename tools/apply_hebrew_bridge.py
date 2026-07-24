#!/usr/bin/env python3
"""Apply sample_data/.../hebrew_invoice_bridge.sql to the local kfar_supply DB."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sample_data" / "kfar_supply" / "git_sql" / "legacy" / "hebrew_invoice_bridge.sql"


def main() -> int:
    load_dotenv(ROOT / ".env")
    cs = os.environ.get("MSSQL_CONNECTION_STRING")
    if not cs:
        print("MSSQL_CONNECTION_STRING missing", file=sys.stderr)
        return 1
    if not SCRIPT.is_file():
        print(f"Missing {SCRIPT}", file=sys.stderr)
        return 1

    text = SCRIPT.read_text(encoding="utf-8")
    batches = [b.strip() for b in re.split(r"(?m)^\s*GO\s*$", text) if b.strip()]

    def _safe(s: object) -> str:
        return str(s).encode("ascii", "backslashreplace").decode("ascii")

    conn = pyodbc.connect(cs, timeout=30, autocommit=True)
    cur = conn.cursor()
    print(f"Applying {len(batches)} batches from {SCRIPT.relative_to(ROOT)}")
    for i, batch in enumerate(batches, 1):
        preview = _safe(" ".join(batch.split())[:90])
        try:
            cur.execute(batch)
            # Consume any result sets (e.g. smoke SELECT)
            while True:
                try:
                    cur.fetchall()
                except pyodbc.ProgrammingError:
                    pass
                if not cur.nextset():
                    break
            print(f"  [{i}/{len(batches)}] OK  {preview}")
        except Exception as e:
            print(f"  [{i}/{len(batches)}] FAIL {preview}\n    {_safe(e)}", file=sys.stderr)
            conn.close()
            return 1

    cur.execute(
        """
        SELECT s.name + N'.' + o.name AS obj, o.type_desc
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        WHERE s.name = N'legacy_hebrew'
        ORDER BY o.type_desc, obj
        """
    )
    rows = cur.fetchall()
    print(f"\nlegacy_hebrew objects ({len(rows)}):")
    for r in rows:
        print(f"  {_safe(r[0])}  {r[1]}")

    cur.execute("SELECT TOP 5 [חשבונית], [סכום] FROM legacy_hebrew.[חשבוניות]")
    sample = cur.fetchall()
    print(f"\nSample rows from invoice bridge view: {sample}")
    conn.close()
    print("Bridge applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
