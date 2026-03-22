from __future__ import annotations

from ama.reports import (
    distinct_merge_table_count,
    format_cli_run_summary,
    merge_scope_metadata,
)


def test_format_cli_all_discovered() -> None:
    payload = {
        "migration_context": "sales.orders",
        "queries_matched": 100,
        "merge_scope": {
            "mode": "all_discovered_tables",
            "tables_merged": 12,
            "merge_cap": 50,
            "comms_git_reference": "sales.orders",
        },
        "column_name_source": "ddl",
        "markdown_sections": {"confirmed": [{"ddl": "x"}]},
    }
    s = format_cli_run_summary(payload, fmt="json")
    assert "Merge scope: 12 tables" in s
    assert "all discovered" in s
    assert "Comms/git reference" in s
    assert "Target table:" not in s


def test_format_cli_single_table_logs() -> None:
    payload = {
        "migration_context": "sales.orders",
        "queries_matched": 0,
        "merge_scope": {
            "mode": "single_table_logs",
            "comms_git_reference": "sales.orders",
            "tables_merged": 1,
        },
        "markdown_sections": {},
    }
    s = format_cli_run_summary(payload, fmt="json")
    assert "Log scope" in s or "single-table" in s


def test_distinct_merge_table_count_multi() -> None:
    assert (
        distinct_merge_table_count(
            {"merged_entities": []},
            multi_merge=True,
            merge_keys=["a.b", "c.d"],
        )
        == 2
    )


def test_merge_scope_metadata_keys() -> None:
    m = merge_scope_metadata(
        discovery_mode=True,
        multi_merge=True,
        discovery_merge_all=True,
        no_target=False,
        merge_keys=["s.t1", "s.t2"],
        migration_context_reference="sales.orders",
        primary_table_key="sales.orders",
        discovery_merge_max=50,
        discovery_merge_n=10,
        tables_merged_distinct=2,
    )
    assert m["mode"] == "all_discovered_tables"
    assert m["merge_cap"] == 50
