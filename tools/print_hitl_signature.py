"""
Print the HITL decision key for a review row (same as ama.business_logic.review_row_signature).

Usage (from repo root):
  python tools/print_hitl_signature.py prod_sales.orders maybe_col amount
"""
from __future__ import annotations

import hashlib
import sys


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python tools/print_hitl_signature.py <source_table> <legacy_name> <suggested_ddl>")
        sys.exit(1)
    raw = "|".join(sys.argv[1:4])
    sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    print(sig)
    print(f"raw string: {raw!r}")


if __name__ == "__main__":
    main()
