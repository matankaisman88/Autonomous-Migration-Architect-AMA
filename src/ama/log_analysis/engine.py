"""
Log Analysis Engine — orchestrates streaming reads and SQL parse telemetry.

This module does **not** load whole log files into memory; it uses
:func:`ama.sql_pipeline.iter_sql_log_record_batches` and incremental chunk updates.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ama.log_analysis.config import LogAnalysisConfig
from ama.parsing.backend import default_parse_backend
from ama.sanitize import sanitize_sql_text
from ama.sql_pipeline import SqlIngestionTelemetry, TableColumnStats, iter_sql_log_record_batches, merge_flat_into_table_stats


@dataclass
class LogAnalysisSummary:
    """Aggregated result of a log scan (serializable)."""

    files: list[str]
    total_rows: int
    telemetry: dict[str, Any]
    distinct_tables: int
    cooccurrence_nonzero: int
    similarity_nonzero: int
    throughput_rows_per_second: float
    peak_memory_mb: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CooccurrenceMatrix:
    """Incremental undirected weighted table co-occurrence matrix."""

    def __init__(self) -> None:
        self._index_by_key: dict[str, int] = {}
        self._keys: list[str] = []
        self._weights: dict[tuple[int, int], int] = {}

    def _idx(self, key: str) -> int:
        i = self._index_by_key.get(key)
        if i is not None:
            return i
        i = len(self._keys)
        self._keys.append(key)
        self._index_by_key[key] = i
        return i

    def update_from_tables(self, tables: set[str]) -> None:
        ids = sorted(self._idx(t) for t in tables if t and "." in t)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                self._weights[(a, b)] = self._weights.get((a, b), 0) + 1

    @property
    def nonzero(self) -> int:
        return len(self._weights)

    @property
    def n_tables(self) -> int:
        return len(self._keys)

    def similarity_nonzero(self, sparse_density_threshold: float) -> tuple[int, str]:
        n = self.n_tables
        if n <= 1 or not self._weights:
            return 0, "none"
        total_pairs = (n * (n - 1)) / 2
        density = (len(self._weights) / total_pairs) if total_pairs else 1.0
        if density <= sparse_density_threshold:
            try:
                import numpy as np
                from scipy import sparse  # type: ignore[import-untyped]

                rows: list[int] = []
                cols: list[int] = []
                data: list[float] = []
                for (a, b), w in self._weights.items():
                    wf = float(w)
                    rows.extend([a, b])
                    cols.extend([b, a])
                    data.extend([wf, wf])
                mat = sparse.csr_matrix((data, (rows, cols)), shape=(n, n), dtype=float)
                # Cosine-style sparse similarity for low-density matrices.
                norms = np.sqrt(mat.power(2).sum(axis=1)).A1
                nz = norms > 0
                inv = np.zeros_like(norms)
                inv[nz] = 1.0 / norms[nz]
                d = sparse.diags(inv)
                sim = d @ mat @ mat.T @ d
                sim = sim.tocsr()
                sim.setdiag(0)
                sim.eliminate_zeros()
                return int(sim.nnz // 2), "scipy_sparse"
            except Exception:
                pass
        # Dense fallback: count potential non-zero similarities via adjacency overlap.
        neigh: dict[int, set[int]] = defaultdict(set)
        for a, b in self._weights:
            neigh[a].add(b)
            neigh[b].add(a)
        nz_pairs = 0
        for i in range(n):
            ni = neigh.get(i, set())
            if not ni:
                continue
            for j in range(i + 1, n):
                if j in ni or (ni & neigh.get(j, set())):
                    nz_pairs += 1
        return nz_pairs, "set_overlap"


class LogAnalysisEngine:
    """
    Facade for analyzing legacy SQL JSONL logs.

    Uses discovery-style ingestion to populate parse telemetry and count distinct
    table keys seen in successfully parsed chunks (streaming).
    """

    def __init__(self, config: LogAnalysisConfig | None = None) -> None:
        self._config = config or LogAnalysisConfig()

    def analyze_paths(
        self,
        paths: list[Path],
        *,
        progress: bool = False,
    ) -> LogAnalysisSummary:
        """
        Stream all given files and return telemetry + approximate table cardinality.

        ``per_table`` stats are merged for counting; only the set of table names is
        summarized in ``distinct_tables``.
        """
        import time
        import tracemalloc

        cfg = self._config
        env = cfg.effective_env()
        telemetry = SqlIngestionTelemetry()
        telemetry_extra: dict[str, Any] = {
            "chunk_size": cfg.chunk_size,
            "chunk_count": 0,
            "last_batch_id": None,
            "last_chunk_id": None,
        }
        parser = default_parse_backend()
        per_table: dict[str, TableColumnStats] = defaultdict(TableColumnStats)
        matrix = CooccurrenceMatrix()

        files_str = [str(p) for p in paths]
        total = 0
        tracemalloc.start()
        t0 = time.perf_counter()
        for path in paths:
            max_r = cfg.max_records_per_file
            batches = iter_sql_log_record_batches(
                path,
                batch_size=cfg.chunk_size,
                max_records=max_r,
            )
            for chunk_idx, batch in enumerate(batches):
                telemetry_extra["chunk_count"] += 1
                for rec in batch:
                    total += 1
                    if progress and total % cfg.progress_every == 0:
                        print(
                            f"log_analysis: processed {total} records (last file {path.name})",
                            file=sys.stderr,
                        )

                    telemetry.total_rows += 1
                    rec_env = rec.get("env")
                    if env and rec_env and str(rec_env).lower() != str(env).lower():
                        telemetry.skipped_env_mismatch += 1
                        continue
                    sql_raw = rec.get("sql") or rec.get("query") or rec.get("statement")
                    had_sql = bool(sql_raw and isinstance(sql_raw, str))
                    if not had_sql:
                        telemetry.skipped_empty_sql += 1
                        continue
                    sql = sanitize_sql_text(str(sql_raw))
                    dialect = rec.get("dialect") if isinstance(rec.get("dialect"), str) else cfg.default_sql_dialect
                    pr = parser.parse(sql, dialect=dialect)
                    telemetry.record_parse_result(pr, had_sql_field=True)
                    if not pr.chunks:
                        continue

                    touched: set[str] = set()
                    for flat in pr.chunks:
                        for tbl in flat:
                            if tbl and "." in tbl:
                                touched.add(tbl)
                    for tbl in touched:
                        per_table[tbl].query_count += 1
                    matrix.update_from_tables(touched)

                    for flat in pr.chunks:
                        for tbl in flat:
                            if tbl and "." in tbl:
                                merge_flat_into_table_stats(flat, tbl, per_table[tbl])

                    b_id = rec.get("batch_id")
                    c_id = rec.get("chunk_id")
                    telemetry_extra["last_batch_id"] = b_id if b_id is not None else total // cfg.chunk_size
                    telemetry_extra["last_chunk_id"] = c_id if c_id is not None else chunk_idx

                if progress and telemetry_extra["chunk_count"] % cfg.progress_chunk_every == 0:
                    print(
                        f"log_analysis: chunk={telemetry_extra['chunk_count']} records={total} "
                        f"(batch_id={telemetry_extra['last_batch_id']} chunk_id={telemetry_extra['last_chunk_id']})",
                        file=sys.stderr,
                    )
        elapsed = max(time.perf_counter() - t0, 1e-9)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        telemetry.total_rows = total
        sim_nz, sim_mode = matrix.similarity_nonzero(cfg.sparse_density_threshold)
        telemetry_dict = telemetry.to_dict()
        telemetry_dict.update(
            {
                "batch_id": telemetry_extra["last_batch_id"],
                "chunk_id": telemetry_extra["last_chunk_id"],
                "chunk_count": telemetry_extra["chunk_count"],
                "chunk_size": cfg.chunk_size,
                "similarity_mode": sim_mode,
            }
        )
        return LogAnalysisSummary(
            files=files_str,
            total_rows=total,
            telemetry=telemetry_dict,
            distinct_tables=len(per_table),
            cooccurrence_nonzero=matrix.nonzero,
            similarity_nonzero=sim_nz,
            throughput_rows_per_second=round(total / elapsed, 2),
            peak_memory_mb=round(peak / (1024 * 1024), 2),
        )
