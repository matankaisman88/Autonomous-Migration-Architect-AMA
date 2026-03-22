"""Tests for merging HITL sidecar into report JSON."""

from __future__ import annotations

from ama.business_logic import review_row_signature
from ama.hitl_apply import apply_hitl_to_report


def _minimal_report() -> dict:
    return {
        "target_table": "s.t",
        "alias_merge": {
            "merged_entities": [],
            "review_candidates": [
                {
                    "legacy_name": "old_a",
                    "suggested_ddl": "new_a",
                    "merge_confidence": 0.55,
                    "category": "review",
                    "citation": "vec",
                    "strategy": "vector",
                    "stats": {"select": 1, "where": 0, "join_on": 0, "group_by": 0, "order_by": 0},
                    "source_table": "s.t",
                },
                {
                    "legacy_name": "old_b",
                    "suggested_ddl": "new_b",
                    "merge_confidence": 0.5,
                    "category": "review",
                    "citation": "weak",
                    "strategy": "vector",
                    "stats": {"select": 0, "where": 0, "join_on": 0, "group_by": 0, "order_by": 0},
                    "source_table": "s.t",
                },
            ],
            "trash_candidates": [],
        },
    }


def test_apply_hitl_approve_moves_to_merged() -> None:
    r = _minimal_report()
    row = r["alias_merge"]["review_candidates"][0]
    sig = review_row_signature(row)
    hitl = {"version": 1, "decisions": {sig: {"action": "approved", "row": {}}}}
    out = apply_hitl_to_report(r, hitl)
    am = out["alias_merge"]
    assert len(am["review_candidates"]) == 1
    assert am["review_candidates"][0]["legacy_name"] == "old_b"
    assert len(am["merged_entities"]) == 1
    assert am["merged_entities"][0]["canonical_column"] == "new_a"
    assert am["merged_entities"][0]["source_columns"] == ["old_a"]
    assert "hitl_approved" in am["merged_entities"][0]["strategies"]


def test_apply_hitl_reject_moves_to_trash() -> None:
    r = _minimal_report()
    row = r["alias_merge"]["review_candidates"][0]
    sig = review_row_signature(row)
    hitl = {"version": 1, "decisions": {sig: {"action": "rejected", "row": {}}}}
    out = apply_hitl_to_report(r, hitl)
    am = out["alias_merge"]
    assert len(am["review_candidates"]) == 1
    assert len(am["trash_candidates"]) == 1
    assert am["trash_candidates"][0]["category"] == "hitl_rejected"
    assert am["trash_candidates"][0]["legacy_name"] == "old_a"


def test_apply_hitl_idempotent_unknown_sig() -> None:
    r = _minimal_report()
    hitl = {"version": 1, "decisions": {"deadbeef" * 8: {"action": "approved"}}}
    out = apply_hitl_to_report(r, hitl)
    assert len(out["alias_merge"]["review_candidates"]) == 2
