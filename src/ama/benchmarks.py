"""
AMA V1 performance benchmarks: Tier 5 (full-database chaos) SQL logs at 10k / 50k / 100k rows.

Metrics: ingestion throughput, Python peak allocation during alias merge (tracemalloc),
optional process RSS (psutil), and hierarchical Excel generation time for 50+ tables.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ama.alias_resolver import AliasResolver, load_ddl_columns, load_glossary
from ama.business_logic import enrich_discovery_business_context
from ama.config import project_root
from ama.discovery import aggregate_merges_for_tables, build_discovery_payload, run_discovery, top_n_tables
from ama.sql_pipeline import TableColumnStats
from ama.reports import write_excel_report

_ROW_COUNTS = (10_000, 50_000, 100_000)


def _load_tier5_generate_lines():
    root = project_root()
    mod_path = root / "tools" / "generate_full_db_chaos.py"
    if not mod_path.is_file():
        raise FileNotFoundError(f"Tier 5 generator not found: {mod_path}")
    spec = importlib.util.spec_from_file_location("ama_tier5_gen", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Tier 5 generator module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.generate_lines


def _rss_mb() -> float:
    try:
        import psutil  # type: ignore[import-untyped]

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line)
            f.write("\n")


def _bench_alias_merge(
    discovery: dict[str, TableColumnStats],
    *,
    ddl_path: Path,
    glossary_paths: list[Path] | None,
) -> tuple[float, float, float, int, dict[str, Any]]:
    """
    Returns (seconds, tracemalloc_peak_mb, rss_delta_mb_or_0, tables_merged, merged_summary).
    Hash-embedding / vector-style scoring runs inside merge_table_stats.
    """
    ddl = load_ddl_columns(ddl_path)
    gp = glossary_paths or []
    resolver = AliasResolver(
        ddl_columns=ddl,
        glossary=load_glossary(*gp),
    )
    keys = top_n_tables(discovery, n=len(discovery))
    gc.collect()
    rss_before = _rss_mb()

    tracemalloc.start()
    t0 = time.perf_counter()
    agg, _combined = aggregate_merges_for_tables(resolver, discovery, keys)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = round(peak / (1024 * 1024), 2)
    rss_after = _rss_mb()
    rss_delta = round(rss_after - rss_before, 2) if rss_after and rss_before else 0.0
    return elapsed, peak_mb, rss_delta, len(keys), agg


def _build_hierarchical_report(
    discovery: dict[str, TableColumnStats],
    merged_summary: dict[str, Any],
    *,
    data_root: Path,
    target_full: str,
) -> dict[str, Any]:
    merge_keys = top_n_tables(discovery, n=len(discovery))
    target_key = merge_keys[0] if merge_keys else ""
    payload = build_discovery_payload(
        discovery,
        target_full,
        target_key,
        None,
        merge_table_keys=merge_keys,
        multi_table_merge=True,
        merged_summary=merged_summary,
        default_database="BENCH_DB",
    )
    enrich_discovery_business_context(payload, data_root=data_root, description_top_n=12)
    qm = sum(int(st.query_count or 0) for st in discovery.values())
    return {
        "target_table": target_full,
        "queries_matched": qm,
        "importance_ddl": [],
        "columns": [],
        "alias_merge": merged_summary,
        "discovery": payload,
    }


def run_benchmark_suite(
    *,
    results_path: Path | None = None,
    data_root: Path | None = None,
) -> int:
    root = (data_root or project_root()).resolve()
    results_path = (results_path or Path.cwd() / "benchmark_results.json").resolve()

    ddl_path = root / "sample_data" / "ddl" / "orders_columns.json"
    if not ddl_path.is_file():
        print(f"error: DDL file required for benchmark: {ddl_path}", file=sys.stderr)
        return 2
    gloss_path = root / "sample_data" / "glossary" / "he_en_columns.json"
    gloss_dirty = root / "sample_data" / "glossary" / "he_en_columns_dirty.json"
    glossary_paths: list[Path] = []
    if gloss_path.is_file():
        glossary_paths.append(gloss_path)
    if gloss_dirty.is_file():
        glossary_paths.append(gloss_dirty)

    generate_lines = _load_tier5_generate_lines()
    runs: list[dict[str, Any]] = []
    excel_block: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="ama_bench_") as tmp:
        tmp_path = Path(tmp)

        last_discovery: dict[str, TableColumnStats] | None = None
        last_agg: dict[str, Any] | None = None

        for n in _ROW_COUNTS:
            print(f"\n--- Benchmark: Tier 5 chaos log, n={n:,} rows ---", flush=True)
            t_gen0 = time.perf_counter()
            lines = generate_lines(n)
            log_path = tmp_path / f"tier5_{n}.jsonl"
            _write_jsonl(log_path, lines)
            gen_sec = time.perf_counter() - t_gen0
            print(f"  Generated + wrote JSONL in {gen_sec:.2f}s ({log_path.name})", flush=True)

            t_ing0 = time.perf_counter()
            discovery = run_discovery([log_path], env=None)
            ingest_sec = time.perf_counter() - t_ing0
            rps = round(n / ingest_sec, 2) if ingest_sec > 0 else 0.0
            n_tables = len(discovery)
            print(f"  Ingestion (discovery scan): {ingest_sec:.2f}s  (~{rps} rows/s), {n_tables} tables", flush=True)

            merge_sec, peak_mb, rss_delta, n_merged, agg = _bench_alias_merge(
                discovery,
                ddl_path=ddl_path,
                glossary_paths=glossary_paths,
            )
            print(
                f"  Alias merge (all tables, vector+lexical in resolver): {merge_sec:.2f}s | "
                f"Python alloc peak ~{peak_mb} MiB | RSS delta ~{rss_delta} MiB",
                flush=True,
            )

            runs.append(
                {
                    "requested_log_rows": n,
                    "tier5_generator_seconds": round(gen_sec, 4),
                    "ingestion_seconds": round(ingest_sec, 4),
                    "ingestion_rows_per_second": rps,
                    "tables_discovered": n_tables,
                    "alias_merge_seconds": round(merge_sec, 4),
                    "alias_merge_tables": n_merged,
                    "memory_python_alloc_peak_mib": peak_mb,
                    "memory_rss_delta_mib": rss_delta,
                }
            )
            last_discovery = discovery
            last_agg = agg

        if last_discovery is not None and last_agg is not None:
            report = _build_hierarchical_report(
                last_discovery,
                last_agg,
                data_root=root,
                target_full="PROD_SALES.Orders",
            )
            inv = report.get("discovery") or {}
            inv_n = len(inv.get("inventory") or [])
            xlsx_path = tmp_path / "bench_hierarchical.xlsx"
            t_x0 = time.perf_counter()
            write_excel_report(report, xlsx_path)
            excel_sec = time.perf_counter() - t_x0
            print(
                f"\n--- Hierarchical Excel ({inv_n} inventory tables): {excel_sec:.2f}s → {xlsx_path.name} ---",
                flush=True,
            )
            excel_block = {
                "inventory_table_count": inv_n,
                "seconds": round(excel_sec, 4),
                "temp_path": str(xlsx_path),
                "note": "Workbook written under a temp directory for timing only.",
            }

    out_doc: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ama_version": "1.0",
        "tier": "tier5_full_db_chaos",
        "data_root": str(root),
        "row_counts": list(_ROW_COUNTS),
        "metrics_help": {
            "ingestion_rows_per_second": "JSONL lines processed per second during discovery scan.",
            "memory_python_alloc_peak_mib": "tracemalloc peak during aggregate alias merge (Python allocator).",
            "memory_rss_delta_mib": "Optional RSS delta if psutil is installed; else 0.",
            "excel": "Time to write hierarchical workbook (discovery enabled, 50+ tables in Tier 5).",
        },
        "runs": runs,
        "excel_hierarchical": excel_block,
    }
    results_path.write_text(json.dumps(out_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n✅ Wrote {results_path}", flush=True)
    return 0
