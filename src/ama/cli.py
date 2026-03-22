from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ama.alias_resolver import AliasResolver, load_ddl_columns, load_glossary
from ama.config import IngestionSettings, project_root
from ama.comms_ingest import aggregate_comms_for_table, iter_comms
from ama.git_ingest import scan_git_sql_roots
from ama.importance import ColumnImportance, compute_importance_v0
from ama.reports import (
    ascii_legacy_names_only,
    format_cli_run_summary,
    is_ascii_identifier,
    legacy_source_summary,
    render_markdown_summary,
    resolve_report_output_path,
    sanitize_citations_for_markdown,
    write_excel_report,
    write_report_file,
)
from ama.sanitize import has_rtl_script, mirror_rtl_identifier_for_ltr_console, normalize_sql_identifier
from pydantic import ValidationError

from ama.schemas.report import (
    AMA_REPORT_SCHEMA_VERSION,
    validate_report_boundary,
    validate_report_model,
)
from ama.sql_pipeline import LineageGraph, SqlIngestionTelemetry, run_sql_logs_pipeline
from ama.business_logic import (
    enrich_discovery_business_context,
    enrich_executive_risk_hotspots,
    infer_default_db_from_data_root,
)
from ama.data_quality import run_dq_suite
from ama.hitl_apply import apply_hitl_to_report, load_hitl_sidecar
from ama.log_analysis import LogAnalysisConfig, LogAnalysisEngine
from ama.planner import AutonomousPlanner
from ama.report_sinks import ExcelReportSink, JsonReportSink
from ama.discovery import (
    aggregate_merges_for_tables,
    build_discovery_payload,
    resolve_target_stats_for_table,
    run_discovery,
    top_n_tables,
)
from ama.sql_pipeline import TableColumnStats
from ama.vector_store import CommsGitVectorStore


def _column_row_for_report(
    r: ColumnImportance,
    *,
    logged_as: list[str] | None = None,
) -> dict[str, object]:
    """
    `column` is the DDL / DB column name when alias merge ran (default).
    For RTL-only names still unmapped to DDL, mirror `column` for LTR consoles and
    keep the logical form as `column_logical`.
    Optional `logged_as`: identifiers seen in SQL logs (Hebrew aliases, typos) merged into this DDL column.
    """
    base = dict(r.__dict__)
    raw = base.get("column")
    if isinstance(raw, str) and has_rtl_script(raw):
        mirrored = mirror_rtl_identifier_for_ltr_console(raw)
        if mirrored != raw:
            out: dict[str, object] = {"column": mirrored, "column_logical": raw}
            for k, v in base.items():
                if k != "column":
                    out[k] = v
            if logged_as is not None:
                out["logged_as"] = logged_as
            return out
    if logged_as is not None:
        base = dict(base)
        base["logged_as"] = logged_as
    return base


def _resolve_ddl_path(root: Path, settings: IngestionSettings, args: argparse.Namespace) -> Path | None:
    if getattr(args, "no_ddl_merge", False):
        return None
    if args.ddl_columns:
        return Path(args.ddl_columns).resolve()
    if settings.ddl_columns_path:
        p = (root / settings.ddl_columns_path).resolve()
        if p.is_file():
            return p
    return None


def _resolve_glossary_path(
    root: Path, settings: IngestionSettings, args: argparse.Namespace
) -> Path | None:
    if args.glossary:
        return Path(args.glossary).resolve()
    if settings.glossary_path:
        p = (root / settings.glossary_path).resolve()
        if p.is_file():
            return p
    return None


def _importance_ddl_only(row: dict[str, object]) -> dict[str, object]:
    """Strip RTL display helpers — Markdown / clean view uses DDL keys only."""
    allowed = (
        "column",
        "log_weight",
        "comms_weight",
        "git_weight",
        "importance_score",
        "dead_candidate",
    )
    return {k: row[k] for k in allowed if k in row}


def _logged_as_for_report(ddl_column: str, sources: list[str] | None) -> list[str] | None:
    """Omit logged_as when the only source matches the DDL name."""
    if not sources:
        return None
    nd = normalize_sql_identifier(ddl_column)
    if len(sources) == 1 and normalize_sql_identifier(sources[0]) == nd:
        return None
    return sources


def _glob_sql_logs(root: Path, pattern: str) -> list[Path]:
    return sorted({p for p in root.glob(pattern) if p.is_file()})


def _resolve_output_spec(args: argparse.Namespace) -> str | None:
    """
    None = print to terminal only (JSON full; Markdown summary).
    Empty string = auto-generate `ama_report_<table>_<timestamp>.<ext>` in cwd.
    """
    if getattr(args, "out_file", None) is not None:
        return args.out_file
    if getattr(args, "out", None):
        return args.out
    return None


def _ensure_utf8_stdout() -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _print_dashboard_hint(*, json_path: Path | None = None) -> None:
    if json_path is not None:
        print(f"\nTo view interactively, run: ama-dashboard --report-path {json_path}")
    else:
        print(
            "\nTo view interactively, export JSON (--format json -o report.json) then run: "
            "ama-dashboard --report-path report.json"
        )


def dashboard_main() -> None:
    """CLI entry: `ama-dashboard --report-path report.json` — launches Streamlit UI."""
    import os
    import subprocess

    argv = sys.argv[1:]
    p = argparse.ArgumentParser(prog="ama-dashboard")
    p.add_argument(
        "--report-path",
        type=str,
        required=True,
        help="Path to the JSON report from ama-ingest run --format json",
    )
    args, rest = p.parse_known_args(argv)
    env = os.environ.copy()
    env["AMA_REPORT_PATH"] = str(Path(args.report_path).resolve())
    app_path = Path(__file__).resolve().parent / "ui" / "dashboard.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path), *rest]
    raise SystemExit(subprocess.call(cmd, env=env))


def cmd_run(args: argparse.Namespace) -> int:
    _ensure_utf8_stdout()
    if getattr(args, "benchmark", False) and getattr(args, "stress", False):
        print("error: use either --benchmark or --stress, not both", file=sys.stderr)
        return 2
    if getattr(args, "benchmark", False):
        from ama.benchmarks import run_benchmark_suite

        br = getattr(args, "benchmark_results", None)
        out = Path(br).expanduser().resolve() if br else Path.cwd() / "benchmark_results.json"
        dr = Path(args.data_root).resolve() if getattr(args, "data_root", None) else None
        return run_benchmark_suite(results_path=out, data_root=dr)
    if getattr(args, "stress", False):
        from ama.stress_monitor import run_stress_ingestion

        return run_stress_ingestion(args)

    root = project_root()
    settings = IngestionSettings()
    if args.data_root:
        root = Path(args.data_root).resolve()

    default_db_resolved = infer_default_db_from_data_root(root, settings.default_db)

    schema = args.target_schema if getattr(args, "target_schema", None) is not None else settings.target_schema
    table = args.target_table if getattr(args, "target_table", None) is not None else settings.target_table
    target = f"{schema}.{table}"
    sql_paths = _glob_sql_logs(root, settings.sql_logs_glob)
    if args.sql_logs:
        sql_paths = [Path(p).resolve() for p in args.sql_logs]

    discovery_mode = getattr(args, "discovery_mode", False)
    no_target = getattr(args, "no_target", False)
    ingest_telemetry = SqlIngestionTelemetry()
    lineage_graph: LineageGraph | None = LineageGraph() if discovery_mode else None
    discovery_tables = None
    target_key = ""
    multi_merge = False
    merge_keys: list[str] = []
    if discovery_mode:
        discovery_tables = run_discovery(
            sql_paths,
            args.env,
            telemetry=ingest_telemetry,
            lineage=lineage_graph,
        )
        if no_target:
            merge_keys = top_n_tables(discovery_tables, n=getattr(args, "discovery_merge_n", 10))
            target_key = merge_keys[0] if merge_keys else ""
            sql_stats = TableColumnStats()
            multi_merge = True
        else:
            target_key, sql_stats = resolve_target_stats_for_table(discovery_tables, target)
    else:
        sql_stats = run_sql_logs_pipeline(
            sql_paths,
            target_full_table=target,
            env=args.env,
            telemetry=ingest_telemetry,
        )

    merged_report: dict | None = None
    logged_by_ddl: dict[str, list[str]] = {}
    mr = None
    ddl_path = _resolve_ddl_path(root, settings, args)
    if ddl_path is not None:
        gloss = _resolve_glossary_path(root, settings, args)
        ddl = load_ddl_columns(ddl_path)
        mf = getattr(args, "merge_floor", None)
        ct = getattr(args, "confirmed_threshold", None)
        resolver = AliasResolver(
            ddl_columns=ddl,
            glossary=load_glossary(gloss),
            merge_floor=float(mf) if mf is not None else settings.merge_confidence_floor,
            confirmed_threshold=float(ct) if ct is not None else settings.merge_confirmed_threshold,
        )
        if discovery_mode and multi_merge and discovery_tables is not None:
            if not merge_keys:
                merged_report = None
                mr = None
            else:
                agg, combined = aggregate_merges_for_tables(resolver, discovery_tables, merge_keys)
                sql_stats = combined
                merged_report = {
                    "ddl_source": str(ddl_path),
                    "column_names_are_ddl": True,
                    "merge_confidence_floor": resolver.merge_floor,
                    "merge_confirmed_threshold": resolver.confirmed_threshold,
                    "merged_entities": agg["merged_entities"],
                    "merge_proposals": agg["merge_proposals"],
                    "review_candidates": agg["review_candidates"],
                    "trash_candidates": agg["trash_candidates"],
                }
                for ent in merged_report["merged_entities"]:
                    k = str(ent.get("canonical_column", ""))
                    src = list(ent.get("source_columns") or [])
                    if k in logged_by_ddl:
                        logged_by_ddl[k] = list(dict.fromkeys(logged_by_ddl[k] + src))
                    else:
                        logged_by_ddl[k] = src
                mr = None
        else:
            mr = resolver.merge_table_stats(sql_stats, source_table=target_key or target)
            for ent in mr.confirmed_entities:
                logged_by_ddl[ent.canonical_column] = list(ent.source_columns)
            merged_report = {
                "ddl_source": str(ddl_path),
                "column_names_are_ddl": True,
                "merge_confidence_floor": resolver.merge_floor,
                "merge_confirmed_threshold": resolver.confirmed_threshold,
                "merged_entities": [asdict(e) for e in mr.confirmed_entities],
                "merge_proposals": [asdict(p) for p in mr.proposals],
                "review_candidates": [asdict(u) for u in mr.review_candidates],
                "trash_candidates": [asdict(u) for u in mr.trash_candidates],
            }
            sql_stats = mr.merged_stats

    discovery_payload: dict[str, object] = {"enabled": False}
    if discovery_mode and discovery_tables is not None:
        discovery_payload = build_discovery_payload(
            discovery_tables,
            target,
            target_key,
            mr,
            merge_table_keys=merge_keys if multi_merge else None,
            multi_table_merge=multi_merge,
            merged_summary=merged_report if multi_merge and merged_report else None,
            default_database=default_db_resolved,
        )
        enrich_discovery_business_context(discovery_payload, data_root=root, description_top_n=10)
        if lineage_graph is not None:
            enrich_executive_risk_hotspots(
                discovery_payload,
                lineage_graph.to_report_dict(),
            )

    comms_dir = root / settings.comms_dir if not args.comms_dir else Path(args.comms_dir)
    comms_score, comms_hits = aggregate_comms_for_table(
        comms_dir,
        schema=settings.target_schema,
        table=settings.target_table,
    )

    git_roots = [root / p for p in settings.git_sql_roots]
    if args.git_root:
        git_roots = [Path(args.git_root)]
    git_total, git_hits = scan_git_sql_roots(
        git_roots,
        schema=settings.target_schema,
        table=settings.target_table,
    )

    store: CommsGitVectorStore | None = None
    if not args.skip_vectors:
        vpath = str(root / settings.qdrant_path) if settings.qdrant_path else None
        store = CommsGitVectorStore(path=vpath, dim=settings.embedding_dim)
        for ch in iter_comms(comms_dir):
            store.upsert_chunk(
                ch.text,
                source=ch.source,
                kind="comms",
                extra={"channel": ch.channel, "ts": ch.ts},
            )
        for hit in git_hits[:50]:
            text = Path(hit.path).read_text(encoding="utf-8", errors="replace")[:12000]
            store.upsert_chunk(
                text,
                source=hit.path,
                kind="git_sql",
                extra={"score": hit.score},
            )

    rows = compute_importance_v0(
        sql_stats,
        comms_score=comms_score,
        comms_chunks=comms_hits,
        git_score=git_total,
        git_hits=git_hits,
    )

    unmapped_importance: list[dict[str, object]] = []
    if mr is not None and mr.unmapped_stats.columns:
        cat_map = {u.legacy_name: u.category for u in mr.review_candidates + mr.trash_candidates}
        unr = compute_importance_v0(
            mr.unmapped_stats,
            comms_score=comms_score,
            comms_chunks=comms_hits,
            git_score=git_total,
            git_hits=git_hits,
        )
        for r in unr:
            row = _column_row_for_report(r)
            row["category"] = cat_map.get(r.column, "unknown")
            unmapped_importance.append(row)

    markdown_sections: dict[str, object] | None = None
    if merged_report is not None:
        me_list = merged_report.get("merged_entities") or []
        confirmed_rows = []
        for e in me_list:
            if isinstance(e, dict):
                confirmed_rows.append(
                    {
                        "ddl": e.get("canonical_column", ""),
                        "source_table": e.get("source_table", ""),
                        "source_count": len(e.get("source_columns") or []),
                        "source_trace": ascii_legacy_names_only(e.get("source_columns") or []),
                        "confidence": round(float(e.get("merge_confidence", 0)), 4),
                        "strategy": ",".join(e.get("strategies") or []) if e.get("strategies") else "merged",
                        "notes": f"{legacy_source_summary(e.get('source_columns') or [])}. "
                        + sanitize_citations_for_markdown(e.get("citations") or []),
                    }
                )
            else:
                confirmed_rows.append(
                    {
                        "ddl": e.canonical_column,
                        "source_table": getattr(e, "source_table", "") or "",
                        "source_count": len(e.source_columns),
                        "source_trace": ascii_legacy_names_only(e.source_columns),
                        "confidence": round(e.merge_confidence, 4),
                        "strategy": ",".join(e.strategies) if e.strategies else "merged",
                        "notes": f"{legacy_source_summary(e.source_columns)}. "
                        + sanitize_citations_for_markdown(e.citations),
                    }
                )
        _imp = {r.column: float(r.importance_score) for r in rows}

        def _imp_key(row: dict[str, object]) -> float:
            ddl = str(row.get("ddl", ""))
            st = str(row.get("source_table", "") or "")
            k = f"{st}::{ddl}" if st else ddl
            return float(_imp.get(k, _imp.get(ddl, -1.0)))

        confirmed_rows.sort(key=lambda r: (-_imp_key(r), str(r.get("ddl", ""))))
        rc_list = merged_report.get("review_candidates") or []
        tr_list = merged_report.get("trash_candidates") or []

        def _uc_dict(u: object) -> dict[str, object]:
            if isinstance(u, dict):
                return {
                    "legacy": u.get("legacy_name", ""),
                    "suggested_ddl": u.get("suggested_ddl", ""),
                    "confidence": round(float(u.get("merge_confidence", 0)), 4),
                    "note": u.get("citation", ""),
                    "source_table": u.get("source_table", ""),
                }
            return {
                "legacy": u.legacy_name,
                "suggested_ddl": u.suggested_ddl,
                "confidence": round(u.merge_confidence, 4),
                "note": u.citation,
                "source_table": getattr(u, "source_table", "") or "",
            }

        markdown_sections = {
            "confirmed": confirmed_rows,
            "review": [_uc_dict(u) for u in rc_list],
            "trash": [_uc_dict(u) for u in tr_list],
        }

    column_rows = [
        _column_row_for_report(
            r,
            logged_as=_logged_as_for_report(r.column, logged_by_ddl.get(r.column)),
        )
        for r in rows
    ]
    importance_ddl = []
    for r in column_rows:
        if not isinstance(r, dict):
            continue
        col = str(r.get("column", ""))
        if "::" in col:
            st_part, tail = col.split("::", 1)
        else:
            st_part, tail = "", col
        if is_ascii_identifier(tail):
            row = _importance_ddl_only(r)
            row = dict(row)
            row["column"] = tail
            if multi_merge and st_part:
                row["source_table"] = st_part
            importance_ddl.append(row)

    lineage_payload = lineage_graph.to_report_dict() if lineage_graph is not None else None
    # Stable keys for dashboard (lineage widgets, risk hotspots) even without --discovery-mode
    if lineage_payload is None:
        lineage_payload = {"edges": [], "edge_count_undirected": 0}

    report = {
        "schema_version": AMA_REPORT_SCHEMA_VERSION,
        "target_table": target,
        "sql_log_files": [str(p) for p in sql_paths],
        "queries_matched": sql_stats.query_count,
        "comms": {"mention_score": comms_score, "chunks_with_hits": comms_hits},
        "git": {"total_score": git_total, "files": [h.__dict__ for h in git_hits[:20]]},
        "columns": column_rows,
        "unmapped_importance": unmapped_importance,
        "vector_points": None,
        "alias_merge": merged_report,
        "column_name_source": "ddl" if merged_report else "log_identifier",
        "markdown_sections": markdown_sections,
        "importance_ddl": importance_ddl,
        "discovery": discovery_payload,
        "lineage": lineage_payload,
    }
    n_err, samples = validate_report_boundary(report)
    ingest_stats = ingest_telemetry.to_dict()
    ingest_stats["report_validation_error_count"] = n_err
    if samples:
        ingest_stats["report_validation_samples"] = samples
    report["ingestion_stats"] = ingest_stats
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        validate_report_model(report)
    except ValidationError as e:
        print(f"warning: report failed ReportModel validation: {e}", file=sys.stderr)
    if store:
        q = f"{settings.target_schema}.{settings.target_table} revenue"
        report["vector_search_demo"] = store.search(q, limit=3)
        report["vector_points"] = "in_memory" if not settings.qdrant_path else str(settings.qdrant_path)

    fmt = getattr(args, "format", "json") or "json"
    out_spec = _resolve_output_spec(args)
    cwd = Path.cwd()

    # Auto-detect Excel when output path ends with .xlsx (default format is json).
    if fmt == "json" and out_spec and out_spec != "":
        probe = Path(out_spec).expanduser()
        if not probe.is_absolute():
            probe = cwd / probe
        if probe.suffix.lower() == ".xlsx":
            fmt = "excel"

    if fmt == "markdown" and out_spec and out_spec != "":
        probe = Path(out_spec).expanduser()
        if not probe.is_absolute():
            probe = cwd / probe
        if probe.suffix.lower() == ".xlsx":
            print(
                "error: --format markdown cannot write to a .xlsx path; use --format excel",
                file=sys.stderr,
            )
            return 2

    if fmt == "markdown":
        ext = ".md"
    elif fmt == "excel":
        ext = ".xlsx"
    else:
        ext = ".json"

    if fmt == "markdown":
        md_out = render_markdown_summary(report)
        if out_spec is not None:
            out_path = resolve_report_output_path(
                out_spec,
                table_full_name=target,
                extension=ext,
                cwd=cwd,
            )
            write_report_file(out_path, md_out)
            print(format_cli_run_summary(report, fmt=fmt, include_markdown_tip=False))
            print(f"\n✅ Report saved to: {out_path}")
            _print_dashboard_hint()
        else:
            print(format_cli_run_summary(report, fmt=fmt, include_markdown_tip=True))
    elif fmt == "excel":
        if out_spec is None:
            print(format_cli_run_summary(report, fmt=fmt, include_markdown_tip=True))
        else:
            out_path = resolve_report_output_path(
                out_spec,
                table_full_name=target,
                extension=ext,
                cwd=cwd,
            )
            write_excel_report(report, out_path)
            print(format_cli_run_summary(report, fmt=fmt, include_markdown_tip=False))
            print(f"\n✅ Report saved to: {out_path}")
            _print_dashboard_hint()
    else:
        out = json.dumps(report, indent=2, ensure_ascii=False)
        if out_spec is not None:
            out_path = resolve_report_output_path(
                out_spec,
                table_full_name=target,
                extension=ext,
                cwd=cwd,
            )
            write_report_file(out_path, out)
            print(format_cli_run_summary(report, fmt=fmt, include_markdown_tip=False))
            print(f"\n✅ Report saved to: {out_path}")
            _print_dashboard_hint(json_path=out_path)
        else:
            try:
                print(out)
            except UnicodeEncodeError:
                sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
                sys.stdout.buffer.write(b"\n")
    return 0


def cmd_apply_hitl(args: argparse.Namespace) -> int:
    """Merge `.hitl.json` decisions into the report so Migration / Excel reflect approvals."""
    report_path = Path(args.report).expanduser().resolve()
    if not report_path.is_file():
        print(f"Report not found: {report_path}", file=sys.stderr)
        return 1

    hitl_path = (
        Path(args.hitl).expanduser().resolve()
        if getattr(args, "hitl", None)
        else report_path.with_suffix(".hitl.json")
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Invalid report JSON: {e}", file=sys.stderr)
        return 1

    hitl = load_hitl_sidecar(hitl_path)
    merged = apply_hitl_to_report(report, hitl)
    merged.setdefault("schema_version", AMA_REPORT_SCHEMA_VERSION)

    fmt = getattr(args, "format", "json") or "json"
    cwd = Path.cwd()
    target = str(merged.get("target_table") or "report")

    if fmt == "excel":
        ext = ".xlsx"
        if args.out_file is not None:
            out_spec = args.out_file
        else:
            out_spec = str(report_path.with_name(f"{report_path.stem}.with_hitl{ext}"))
        out_path = ExcelReportSink().write(merged, target=target, out_spec=out_spec, cwd=cwd)
        print(f"Applied HITL ({hitl_path.name}) → Excel: {out_path}")
    else:
        ext = ".json"
        if args.out_file is not None:
            out_spec = args.out_file
        else:
            out_spec = str(report_path.with_name(f"{report_path.stem}.with_hitl{ext}"))
        out_path = JsonReportSink().write(merged, target=target, out_spec=out_spec, cwd=cwd)
        print(f"Applied HITL ({hitl_path.name}) → JSON: {out_path}")
        _print_dashboard_hint(json_path=out_path)

    return 0


def cmd_dq(args: argparse.Namespace) -> int:
    """Run DQ suite on an AMA report JSON."""
    report_path = Path(args.report).expanduser().resolve()
    if not report_path.is_file():
        print(f"Report not found: {report_path}", file=sys.stderr)
        return 1
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Invalid report JSON: {e}", file=sys.stderr)
        return 1
    result = run_dq_suite(report)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


def cmd_plan(args: argparse.Namespace) -> int:
    """Emit migration plan JSON derived from discovery inventory in a report."""
    report_path = Path(args.report).expanduser().resolve()
    if not report_path.is_file():
        print(f"Report not found: {report_path}", file=sys.stderr)
        return 1
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Invalid report JSON: {e}", file=sys.stderr)
        return 1
    plan = AutonomousPlanner().plan_from_report(
        report,
        max_tables_per_wave=getattr(args, "max_tables_per_wave", 25),
        max_waves=getattr(args, "max_waves", 20),
    )
    print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_log_scan(args: argparse.Namespace) -> int:
    """Stream-scan SQL JSONL files and print LogAnalysisSummary JSON."""
    paths = [Path(p).expanduser().resolve() for p in (args.sql_logs or [])]
    for p in paths:
        if not p.is_file():
            print(f"Not a file: {p}", file=sys.stderr)
            return 1
    if getattr(args, "all_envs", False):
        env: str | None = None
    else:
        raw = getattr(args, "env", None)
        env = None if raw in (None, "") else str(raw)
    cfg = LogAnalysisConfig(
        env_filter=env,
        max_records_per_file=args.max_records,
        progress_every=args.progress_every,
    )
    eng = LogAnalysisEngine(cfg)
    summary = eng.analyze_paths(paths, progress=args.progress)
    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        prog="ama-ingest",
        description="Autonomous Migration Architect — SQL ingestion, reports, benchmarks",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Run SQL + comms + git + importance v0")
    r.add_argument("--data-root", type=str, default=None, help="Project root (default: package root)")
    r.add_argument("--sql-logs", nargs="*", help="Explicit SQL log JSONL files")
    r.add_argument("--comms-dir", type=str, default=None)
    r.add_argument("--git-root", type=str, default=None)
    r.add_argument("--env", type=str, default="prod", help="Filter sql log env (use '' for all)")
    r.add_argument("--skip-vectors", action="store_true")
    r.add_argument(
        "--out-file",
        "-o",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "Write report to PATH (Markdown, JSON, or Excel .xlsx). "
            "Paths ending in .xlsx use Excel format when --format is the default (json). "
            "Use -o / --out-file without PATH for ama_report_<table>_<timestamp>.<ext> in the current directory."
        ),
    )
    r.add_argument(
        "--out",
        type=str,
        default=None,
        help="Deprecated: same as --out-file PATH",
    )
    r.add_argument(
        "--ddl-columns",
        type=str,
        default=None,
        help="JSON file: list of DDL columns or {columns: [...]} for alias merge",
    )
    r.add_argument("--glossary", type=str, default=None, help="Hebrew/English column glossary JSON")
    r.add_argument(
        "--no-ddl-merge",
        action="store_true",
        help="Do not load DDL (disable canonical DB column names in the report)",
    )
    r.add_argument(
        "--format",
        choices=("json", "markdown", "excel"),
        default="json",
        help="Output: JSON (default), Markdown summary, or Excel workbook (.xlsx)",
    )
    r.add_argument(
        "--merge-floor",
        type=float,
        default=None,
        help="Minimum merge confidence to map a log column onto DDL (default: AMA_MERGE_CONFIDENCE_FLOOR / 0.4)",
    )
    r.add_argument(
        "--confirmed-threshold",
        type=float,
        default=None,
        help="Vector matches must be >= this to merge onto DDL (default: 0.8); glossary/exact always merge",
    )
    r.add_argument(
        "--discovery-mode",
        action="store_true",
        help="Scan all database.schema.table references in SQL logs; DDL merge applies to the configured target table only",
    )
    r.add_argument(
        "--no-target",
        action="store_true",
        help="With --discovery-mode: do not pin merge to target schema/table; merge the Top N busiest tables (see --discovery-merge-n)",
    )
    r.add_argument(
        "--discovery-merge-n",
        type=int,
        default=10,
        help="With --discovery-mode --no-target: how many top tables to run DDL merge on (default: 10)",
    )
    r.add_argument(
        "--target-schema",
        type=str,
        default=None,
        help="Override AMA_TARGET_SCHEMA for single-table / discovery target resolution",
    )
    r.add_argument(
        "--target-table",
        type=str,
        default=None,
        help="Override AMA_TARGET_TABLE (single-table mode or discovery anchor when not using --no-target)",
    )
    r.add_argument(
        "--benchmark",
        action="store_true",
        help="Run Tier 5 performance benchmark (10k/50k/100k rows); writes benchmark_results.json; ignores other run options",
    )
    r.add_argument(
        "--benchmark-results",
        type=str,
        default=None,
        metavar="PATH",
        help="Output path for benchmark JSON (default: ./benchmark_results.json in cwd)",
    )
    r.add_argument(
        "--stress",
        action="store_true",
        help="Extreme stress: batched discovery on AMA_STRESS_LOG or chaos_data/sql_logs/extreme_1m.jsonl; writes stress_report.json",
    )
    r.add_argument(
        "--stress-lines",
        type=int,
        default=None,
        metavar="N",
        help="Max JSON records to process per file (default: all). Overrides AMA_STRESS_MAX_LINES.",
    )
    r.add_argument(
        "--stress-report",
        type=str,
        default=None,
        metavar="PATH",
        help="Output JSON for peak memory & timing (default: ./stress_report.json)",
    )
    r.add_argument(
        "--stress-batch-size",
        type=int,
        default=5000,
        metavar="N",
        help="Records per batch when --stress is set (default: 5000)",
    )
    r.set_defaults(func=cmd_run)

    h = sub.add_parser(
        "apply-hitl",
        help="Apply Review (HITL) decisions from <report>.hitl.json into the report JSON / Excel",
    )
    h.add_argument(
        "--report",
        type=str,
        required=True,
        help="Path to the ingestion report JSON (same file you load in the dashboard)",
    )
    h.add_argument(
        "--hitl",
        type=str,
        default=None,
        help="Path to the sidecar .hitl.json (default: <report>.hitl.json next to the report)",
    )
    h.add_argument(
        "--out-file",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help="Output path (.json or .xlsx). Default: <report_stem>.with_hitl.json / .xlsx",
    )
    h.add_argument(
        "--format",
        choices=("json", "excel"),
        default="json",
        help="Write merged JSON or Excel workbook (Migration sheet uses merged_entities)",
    )
    h.set_defaults(func=cmd_apply_hitl)

    dq = sub.add_parser(
        "dq",
        help="Run data quality checks on an AMA report JSON (schema boundary, ingestion_stats, discovery)",
    )
    dq.add_argument(
        "--report",
        type=str,
        required=True,
        help="Path to report.json from ama-ingest run --format json",
    )
    dq.set_defaults(func=cmd_dq)

    pl = sub.add_parser(
        "plan",
        help="Print an autonomous migration plan (JSON) from discovery inventory in a report",
    )
    pl.add_argument(
        "--report",
        type=str,
        required=True,
        help="Path to report.json (use discovery-mode ingestion for inventory)",
    )
    pl.add_argument(
        "--max-tables-per-wave",
        type=int,
        default=25,
        metavar="N",
        help="Split large domains into multiple waves (default: 25)",
    )
    pl.add_argument(
        "--max-waves",
        type=int,
        default=20,
        metavar="N",
        help="Cap total waves (default: 20)",
    )
    pl.set_defaults(func=cmd_plan)

    ls = sub.add_parser(
        "log-scan",
        help="Stream-scan SQL JSONL log files and print parse telemetry (no full ingest report)",
    )
    ls.add_argument(
        "sql_logs",
        nargs="+",
        metavar="PATH",
        help="One or more .jsonl SQL log files",
    )
    ls.add_argument(
        "--env",
        type=str,
        default="prod",
        help="Filter JSONL rows by env (default: prod)",
    )
    ls.add_argument(
        "--all-envs",
        action="store_true",
        help="Do not filter by env (include all rows)",
    )
    ls.add_argument(
        "--max-records",
        type=int,
        default=None,
        metavar="N",
        help="Max records per file (default: all)",
    )
    ls.add_argument(
        "--progress",
        action="store_true",
        help="Print progress to stderr every N records",
    )
    ls.add_argument(
        "--progress-every",
        type=int,
        default=50_000,
        metavar="N",
        help="With --progress, emit every N records (default: 50000)",
    )
    ls.set_defaults(func=cmd_log_scan)

    args = p.parse_args()
    if args.cmd == "run" and args.env == "":
        args.env = None

    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
