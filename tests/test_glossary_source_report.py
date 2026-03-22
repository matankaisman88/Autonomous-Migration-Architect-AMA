from __future__ import annotations

import json
from pathlib import Path

from ama.business_logic import build_business_glossary_entries, build_glossary_source_report

ROOT = Path(__file__).resolve().parents[1]


def test_build_glossary_source_report_counts(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"foo": "bar", "_x": "y"}, ensure_ascii=False), encoding="utf-8")
    r = build_glossary_source_report(tmp_path, [a])
    assert r["total_entries"] == 1
    assert len(r["layers"]) == 1
    assert r["layers"][0]["entries"][0]["source_term"] == "foo"


def test_business_glossary_includes_source_layer() -> None:
    report = {
        "alias_merge": {"merged_entities": [], "review_candidates": []},
        "glossary_source": {
            "layers": [
                {
                    "file": "he_en_columns.json",
                    "path_relative": "sample_data/glossary/he_en_columns.json",
                    "layer": "clean",
                    "entries": [{"source_term": "סכום", "target_column": "amount"}],
                }
            ],
            "total_entries": 1,
        },
    }
    g = build_business_glossary_entries(report)
    assert any(str(x.get("kind")) == "glossary_source" for x in g)
    assert any(x.get("target_ddl") == "amount" for x in g)


def _count_glossary_pairs(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    return sum(
        1
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")
    )


def test_repo_glossary_files_full_coverage() -> None:
    clean = ROOT / "sample_data" / "glossary" / "he_en_columns.json"
    dirty = ROOT / "sample_data" / "glossary" / "he_en_columns_dirty.json"
    if not clean.is_file() or not dirty.is_file():
        return
    r = build_glossary_source_report(ROOT, [clean, dirty])
    assert r["total_entries"] == _count_glossary_pairs(clean) + _count_glossary_pairs(dirty)
