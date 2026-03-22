from __future__ import annotations

from pathlib import Path

from ama.business_logic import (
    _heuristic_domain,
    enrich_discovery_business_context,
    infer_default_db_from_data_root,
)


def test_infer_default_db_explicit() -> None:
    assert infer_default_db_from_data_root(Path("/x/y/chaos_data"), "MYDB") == "MYDB"


def test_infer_default_db_from_folder() -> None:
    p = Path("C:/projects/chaos_data")
    assert infer_default_db_from_data_root(p, None) == "chaos_data"


def test_heuristic_domain_finance() -> None:
    assert _heuristic_domain("PROD_SALES", "Invoices", "PROD_SALES.Invoices", "") == "Finance"


def test_heuristic_domain_technical_debt() -> None:
    assert _heuristic_domain("TEMP_JUNK", "Tmp_1", "TEMP_JUNK.Tmp_1", "") == "Technical Debt"


def test_enrich_adds_domains(tmp_path: Path) -> None:
    disc = {
        "enabled": True,
        "inventory": [
            {
                "database": "",
                "schema": "PROD_SALES",
                "table": "Orders",
                "full_name": "PROD_SALES.Orders",
                "query_count": 50,
                "column_count": 2,
                "priority_score": 100.0,
                "status": "Discovered (not in DDL scope)",
            },
            {
                "database": "",
                "schema": "TEMP_JUNK",
                "table": "Tmp_1",
                "full_name": "TEMP_JUNK.Tmp_1",
                "query_count": 1,
                "column_count": 1,
                "priority_score": 2.0,
                "status": "Ephemeral (Temp)",
            },
        ],
    }
    out = enrich_discovery_business_context(disc, data_root=tmp_path, description_top_n=2)
    inv = out["inventory"]
    assert inv[0]["portfolio_section"] == "Core Business"
    assert inv[-1]["portfolio_section"] == "Technical Debt"
    assert "executive_summary" in out
    assert len(out["executive_summary"]["domain_matrix"]) >= 1
