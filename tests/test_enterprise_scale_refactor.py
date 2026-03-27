from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from ama.ddl_manifest import extract_manifest_entries_from_ddl_sql, resolve_table_metadata_for_key
from ama.log_analysis import LogAnalysisConfig, LogAnalysisEngine

ROOT = Path(__file__).resolve().parents[1]


def _load_chaos_factory():
    mod_path = ROOT / "tools" / "generate_extreme_chaos.py"
    spec = importlib.util.spec_from_file_location("ama_extreme_chaos", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load generate_extreme_chaos module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.ChaosFactory


def test_chaos_factory_generates_1000_tables_across_3_schemas() -> None:
    ChaosFactory = _load_chaos_factory()
    fac = ChaosFactory(scale=1000, source_dialect="oracle")
    assert len(fac._tables) == 1000  # internal invariant for scale generation
    schemas = sorted({t.schema for t in fac._tables})
    assert len(schemas) == 3
    ddl = "\n".join(fac.ddl_statements()[:10])
    assert "CREATE SEQUENCE" in ddl
    assert "TABLESPACE" in ddl


def test_oracle_ddl_maps_to_table_metadata() -> None:
    ddl = """
    CREATE TABLE FINANCE.ORDERS (
      ID NUMBER(19) PRIMARY KEY,
      STATUS VARCHAR2(20)
    ) TABLESPACE TS_FIN_01;
    """
    entries = extract_manifest_entries_from_ddl_sql(
        ddl,
        source_dialect="oracle",
        ddl_path="sample_data/ddl/orders_columns.json",
    )
    md = resolve_table_metadata_for_key(entries, "finance.orders")
    assert md is not None
    assert md.table_key == "finance.orders"
    assert md.source_dialect == "oracle"
    assert md.owner == "finance"
    assert md.tablespace == "ts_fin_01"


def test_log_analysis_idempotent_weights_and_chunk_telemetry(tmp_path: Path) -> None:
    p = tmp_path / "logs.jsonl"
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for i in range(10_000):
            rec = {
                "env": "prod",
                "dialect": "oracle",
                "batch_id": i // 1000,
                "chunk_id": i // 500,
                "sql": (
                    "SELECT a.ID, b.ID FROM FINANCE.ORDERS a "
                    "JOIN FINANCE.ORDER_LINES b ON a.ID=b.PARENT_ID "
                    f"WHERE a.SHARD_KEY = {i % 97}"
                ),
            }
            f.write(json.dumps(rec) + "\n")

    cfg = LogAnalysisConfig(env_filter="prod", chunk_size=2000)
    eng = LogAnalysisEngine(cfg)
    s1 = eng.analyze_paths([p], progress=False)
    s2 = eng.analyze_paths([p], progress=False)

    assert s1.cooccurrence_nonzero == s2.cooccurrence_nonzero
    assert s1.similarity_nonzero == s2.similarity_nonzero
    assert s1.distinct_tables == s2.distinct_tables
    assert s1.telemetry.get("batch_id") is not None
    assert s1.telemetry.get("chunk_id") is not None
    # Streaming guard: for this fixture scale, memory should remain modest.
    assert s1.peak_memory_mb < 512
