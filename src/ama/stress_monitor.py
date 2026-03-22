"""
Resource monitoring for extreme SQL log stress runs (OOM warnings, throughput, peak RSS).
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from ama.config import project_root
from ama.discovery import run_discovery


def _rss_mb() -> float:
    try:
        import psutil  # type: ignore[import-untyped]

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 3)
    except Exception:
        return 0.0


def _vm_percent() -> float:
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


class StressMonitor:
    """Track peak RSS and warn before likely OOM (host-level memory pressure)."""

    def __init__(
        self,
        *,
        rss_warn_percent: float = 90.0,
        vm_warn_percent: float = 92.0,
    ) -> None:
        self.rss_warn_percent = rss_warn_percent
        self.vm_warn_percent = vm_warn_percent
        self.peak_rss_mb = 0.0
        self.start_rss_mb = 0.0
        self._oom_warned = False
        self._samples: list[tuple[float, float, float]] = []
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self.start_rss_mb = _rss_mb()
        self.peak_rss_mb = self.start_rss_mb

    def sample(self, *, records_so_far: int) -> None:
        rss = _rss_mb()
        self.peak_rss_mb = max(self.peak_rss_mb, rss)
        vm = _vm_percent()
        now = time.perf_counter()
        self._samples.append((now - self._t0, float(records_so_far), rss))
        if not self._oom_warned and vm >= self.vm_warn_percent:
            self._oom_warned = True
            print(
                f"\nwarning: system memory pressure is high ({vm:.1f}% of RAM). "
                "Consider stopping other apps, or use AMA_STRESS_MAX_LINES / --stress-lines to cap work.",
                file=sys.stderr,
            )

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def throughput(self, records: int) -> float:
        e = self.elapsed()
        return round(records / e, 2) if e > 0 else 0.0

    def summary(self, *, records_processed: int) -> dict[str, Any]:
        e = self.elapsed()
        return {
            "elapsed_seconds": round(e, 3),
            "records_processed": records_processed,
            "records_per_second": round(records_processed / e, 2) if e > 0 else 0.0,
            "rss_start_mb": self.start_rss_mb,
            "rss_peak_mb": self.peak_rss_mb,
            "rss_delta_peak_mb": round(self.peak_rss_mb - self.start_rss_mb, 3),
            "oom_warning_emitted": self._oom_warned,
        }


def run_stress_ingestion(args: Namespace) -> int:
    """
    Run discovery on the extreme stress log with batched streaming + tqdm + monitoring.
    Writes JSON summary (default: ./stress_report.json).
    """
    if getattr(args, "benchmark", False):
        print("error: use either --benchmark or --stress, not both", file=sys.stderr)
        return 2

    root = Path(args.data_root).resolve() if getattr(args, "data_root", None) else project_root()
    env_path = os.environ.get("AMA_STRESS_LOG", "").strip()
    if env_path:
        log_path = Path(env_path).expanduser().resolve()
    else:
        log_path = root / "chaos_data" / "sql_logs" / "extreme_1m.jsonl"

    if not log_path.is_file():
        print(
            f"error: stress log not found: {log_path}\n"
            "Generate it first:\n"
            "  python tools/generate_extreme_chaos.py --lines 1000000 --out chaos_data/sql_logs/extreme_1m.jsonl\n"
            "Or set AMA_STRESS_LOG to an existing JSONL path.",
            file=sys.stderr,
        )
        return 2

    max_lines_env = os.environ.get("AMA_STRESS_MAX_LINES", "").strip()
    max_records: int | None = getattr(args, "stress_lines", None)
    if max_lines_env.isdigit():
        max_records = int(max_lines_env)
    batch_size = int(getattr(args, "stress_batch_size", 5000) or 5000)

    out_report = Path(
        getattr(args, "stress_report", None) or os.environ.get("AMA_STRESS_REPORT", "") or "stress_report.json"
    ).expanduser()
    if not out_report.is_absolute():
        out_report = Path.cwd() / out_report

    monitor = StressMonitor()
    monitor.start()
    counter: list[int] = [0]

    def on_batch(_bi: int) -> None:
        monitor.sample(records_so_far=counter[0])

    def _env() -> str | None:
        e = getattr(args, "env", "prod")
        return None if e == "" else e

    print(f"Stress load: {log_path}", flush=True)
    print(f"Batch size: {batch_size} | progress: tqdm | max records: {max_records or 'all'}", flush=True)

    t0 = time.perf_counter()
    tables = run_discovery(
        [log_path],
        _env(),
        batch_size=batch_size,
        progress=True,
        max_records_per_file=max_records,
        on_batch_complete=on_batch,
        records_counter=counter,
    )
    elapsed = time.perf_counter() - t0
    monitor.sample(records_so_far=counter[0])

    n_tables = len(tables)
    n_queries = sum(int(st.query_count or 0) for st in tables.values())

    doc = {
        "stress_log": str(log_path),
        "data_root": str(root),
        "batch_size": batch_size,
        "records_processed": counter[0],
        "monitor": monitor.summary(records_processed=counter[0]),
        "elapsed_wall_seconds": round(elapsed, 3),
        "discovery_tables": n_tables,
        "total_query_events": n_queries,
    }
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n✅ Stress run complete. Peak RSS ~{monitor.peak_rss_mb:.1f} MiB | {counter[0]:,} records in {elapsed:.1f}s")
    print(f"✅ Wrote {out_report.resolve()}", flush=True)
    return 0
