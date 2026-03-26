from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class MigrationContract:
    rules: list[str]
    contract_id: str
    table_count: int
    excluded: list[str]


def build_contract(
    *,
    green_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
) -> MigrationContract:
    rules: set[str] = set()
    excluded: list[str] = []
    green_keys = {str(r.get("table_key") or "") for r in green_rows}
    for row in green_rows:
        table_key = str(row.get("table_key") or "")
        comp = row.get("confidence_components") if isinstance(row.get("confidence_components"), dict) else {}
        crit = row.get("criticality_components") if isinstance(row.get("criticality_components"), dict) else {}
        rules.add(
            f"{table_key}: glossary={int(comp.get('glossary_match', 0))}, "
            f"type_pattern={int(comp.get('type_pattern', 0))}, "
            f"lineage={int(crit.get('lineage', 0))}, usage={int(crit.get('usage', 0))}"
        )

    for row in all_rows:
        table_key = str(row.get("table_key") or "")
        if table_key in green_keys:
            continue
        excluded.append(table_key)

    rules_sorted = sorted(rules)
    canonical = json.dumps(rules_sorted, ensure_ascii=False)
    contract_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return MigrationContract(
        rules=rules_sorted,
        contract_id=contract_id,
        table_count=len(green_rows),
        excluded=sorted(set(excluded)),
    )
