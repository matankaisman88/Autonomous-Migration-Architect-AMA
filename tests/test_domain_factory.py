"""Tests for multi-domain fixture generator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_domain_data",
        Path(__file__).resolve().parents[1] / "tools" / "generate_domain_data.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    DomainFactory = mod.DomainFactory
    SKIP = False
except Exception:
    SKIP = True


@pytest.mark.skipif(SKIP, reason="generate_domain_data.py not yet created")
class TestDomainFactory:

    def test_finance_sandbox_structure(self, tmp_path: Path) -> None:
        factory = DomainFactory("finance", seed=42)
        sandbox = factory.generate(n_lines=500, out_parent=tmp_path)
        assert (sandbox / "ddl" / "manifest.json").is_file()
        assert (sandbox / "glossary").is_dir()
        assert any((sandbox / "sql_logs").glob("*.jsonl"))
        assert (sandbox / "comms").is_dir()
        assert (sandbox / "git_sql").is_dir()
        assert (sandbox / "README.md").is_file()

    def test_hr_sandbox_sql_log_is_valid_jsonl(self, tmp_path: Path) -> None:
        factory = DomainFactory("hr", seed=42)
        sandbox = factory.generate(n_lines=300, out_parent=tmp_path)
        log = next((sandbox / "sql_logs").glob("*.jsonl"))
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 200
        for line in lines[:10]:
            row = json.loads(line)
            assert "sql" in row and "dialect" in row and "env" in row

    def test_manifest_entries_resolve(self, tmp_path: Path) -> None:
        factory = DomainFactory("logistics", seed=42)
        sandbox = factory.generate(n_lines=300, out_parent=tmp_path)
        manifest = json.loads((sandbox / "ddl" / "manifest.json").read_text())
        for k, v in manifest.items():
            if k.startswith("_"):
                continue
            ddl_path = sandbox / v if not Path(v).is_absolute() else Path(v)
            if not ddl_path.is_file():
                ddl_path = sandbox.parent / v
            assert ddl_path.is_file() or (sandbox / v.lstrip("/")).is_file(), (
                f"DDL file missing for {k}: {v}"
            )

    def test_glossary_covers_domain_hebrew_terms(self, tmp_path: Path) -> None:
        factory = DomainFactory("hr", seed=42)
        sandbox = factory.generate(n_lines=300, out_parent=tmp_path)
        glossary_files = list((sandbox / "glossary").glob("*_glossary.json"))
        assert glossary_files
        glossary = json.loads(glossary_files[0].read_text(encoding="utf-8"))
        assert "שם_פרטי" in glossary
        assert "מחלקה" in glossary

    def test_all_domains_generate_without_error(self, tmp_path: Path) -> None:
        for domain in ["finance", "hr", "logistics", "retail", "healthcare"]:
            factory = DomainFactory(domain, seed=42)
            sandbox = factory.generate(n_lines=200, out_parent=tmp_path)
            assert sandbox.is_dir(), f"Sandbox not created for domain {domain}"

    def test_two_runs_same_seed_are_identical(self, tmp_path: Path) -> None:
        f1 = DomainFactory("retail", seed=99)
        s1 = f1.generate(n_lines=300, out_parent=tmp_path / "run1")
        f2 = DomainFactory("retail", seed=99)
        s2 = f2.generate(n_lines=300, out_parent=tmp_path / "run2")
        log1 = next((s1 / "sql_logs").glob("*.jsonl")).read_text(encoding="utf-8")
        log2 = next((s2 / "sql_logs").glob("*.jsonl")).read_text(encoding="utf-8")
        assert log1 == log2, "Same seed must produce identical SQL log content"

    def test_bilingual_probe_produces_correct_cooccurrence(self, tmp_path: Path) -> None:
        """The HR domain bilingual probes must yield שם_פרטי->first_name pairs."""
        from ama.glossary.cooccurrence import mine_cooccurrences

        factory = DomainFactory("hr", seed=42)
        sandbox = factory.generate(n_lines=1000, out_parent=tmp_path)

        log = next((sandbox / "sql_logs").glob("*.jsonl"))
        manifest = json.loads((sandbox / "ddl" / "manifest.json").read_text())
        all_cols: list[str] = []
        for k, v in manifest.items():
            if k.startswith("_"):
                continue
            ddl_file = sandbox / v
            if ddl_file.is_file():
                raw = json.loads(ddl_file.read_text())
                cols = raw if isinstance(raw, list) else raw.get("columns", [])
                all_cols.extend(cols)

        pairs = mine_cooccurrences([log], list(set(all_cols)), env_filter="prod")
        assert "שם_פרטי" in pairs, f"שם_פרטי not found in pairs. All RTL tokens: {list(pairs.keys())}"
        assert "first_name" in pairs["שם_פרטי"], (
            f"first_name not in pairs for שם_פרטי. Got: {pairs.get('שם_פרטי')}"
        )
