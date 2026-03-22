#!/usr/bin/env python3
"""
AMA Live Demo — orchestrated CLI showcase for stakeholders.

Runs the same pipeline as::

    ama-ingest run --discovery-mode --discovery-merge-all --format json -o <report>

then computes a migration plan snapshot and optionally opens the Streamlit dashboard.

Usage:
  python demo_runner.py
  python demo_runner.py --no-dashboard
  python demo_runner.py --all-project-sql-logs
  python demo_runner.py --skip-vectors
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Optional: rich (declared in pyproject.toml)
# -----------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from rich.table import Table
except ImportError as e:
    print("Install dependencies: pip install -e .", file=sys.stderr)
    raise SystemExit(1) from e


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _ingest_args(
    *,
    data_root: Path,
    out_report: Path,
    sql_logs: list[str] | None,
    skip_vectors: bool,
    discovery_merge_max: int | None,
) -> argparse.Namespace:
    """Mirror ``ama-ingest run`` for :func:`ama.cli.cmd_run`."""
    return argparse.Namespace(
        benchmark=False,
        benchmark_results=None,
        stress=False,
        stress_lines=None,
        stress_report=None,
        stress_batch_size=5000,
        data_root=str(data_root),
        sql_logs=sql_logs,
        comms_dir=None,
        git_root=None,
        env="prod",
        skip_vectors=skip_vectors,
        out_file=str(out_report),
        out=None,
        format="json",
        ddl_columns=None,
        ddl_manifest=None,
        glossary=None,
        glossary_dirty=None,
        no_ddl_merge=False,
        merge_floor=None,
        confirmed_threshold=None,
        discovery_mode=True,
        no_target=False,
        discovery_merge_all=True,
        discovery_merge_n=10,
        discovery_merge_max=discovery_merge_max,
        target_schema=None,
        target_table=None,
    )


def _complexity_for_table(full_name: str, business_domain: str, report: dict[str, Any]) -> str:
    es = (report.get("discovery") or {}).get("executive_summary") or {}
    for row in es.get("domain_matrix") or []:
        if isinstance(row, dict) and str(row.get("business_domain") or "") == business_domain:
            return str(row.get("migration_complexity", "-"))
    return "-"


def _run_demo(
    *,
    data_root: Path,
    out_report: Path,
    sql_logs: list[str] | None,
    skip_vectors: bool,
    discovery_merge_max: int | None,
    launch_dashboard: bool,
    console: Console,
) -> int:
    from ama.cli import cmd_run
    from ama.planner import AutonomousPlanner

    console.print("[bold cyan]AMA - Autonomous Migration Architect[/bold cyan] [dim]live demo[/dim]\n")

    run_args = _ingest_args(
        data_root=data_root,
        out_report=out_report,
        sql_logs=sql_logs,
        skip_vectors=skip_vectors,
        discovery_merge_max=discovery_merge_max,
    )

    # --- Step 1: full ingest (same as ama-ingest run --discovery-mode --discovery-merge-all)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        t1 = progress.add_task(
            "[green]Step 1/2[/green]  Full ingest (discovery + merge-all + comms + git)...",
            total=1,
        )
        console.log(
            "[dim]Validating SQLGlot AST; multi-table DDL merge via ddl-manifest; "
            "enriching discovery; optional vectors...[/dim]"
        )
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_run(run_args)
        progress.update(t1, completed=1)

    if rc != 0:
        console.print(
            Panel(
                f"[red]ama-ingest exited {rc}[/red]\n{err_buf.getvalue() or out_buf.getvalue()}",
                title="Ingest failed",
                border_style="red",
            )
        )
        return rc

    console.log("[green]OK[/green] Ingest complete ([bold]--discovery-mode --discovery-merge-all[/bold]).")

    # Load report written by cmd_run
    try:
        report = json.loads(out_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        console.print(Panel(f"[red]Could not read report:[/red] {e}", border_style="red"))
        return 1

    # --- Step 2: migration plan snapshot (LineageOrder + waves)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        t2 = progress.add_task("[green]Step 2/2[/green]  Migration plan (lineage DAG + domain waves)...", total=1)
        console.log("[dim]Analyzing DAG dependencies; partitioning waves by domain...[/dim]")
        plan = AutonomousPlanner().plan_from_report(report, max_tables_per_wave=25, max_waves=50)
        progress.update(t2, completed=1)

    console.log("[green]OK[/green] Migration plan snapshot ready.")

    # --- Summary table --------------------------------------------------------------
    summary = Table(title="Demo summary - inventory scope", show_lines=True, header_style="bold magenta")
    summary.add_column("Migrated tables", style="cyan", no_wrap=True)
    summary.add_column("Complexity score", justify="right", style="yellow")
    summary.add_column("Action status", style="green")

    inv = (report.get("discovery") or {}).get("inventory") or []
    for row in inv[:40]:
        if not isinstance(row, dict):
            continue
        fn = str(row.get("full_name") or "")
        dom = str(row.get("business_domain") or "-")
        st = str(row.get("status") or "-")
        cx = _complexity_for_table(fn, dom, report)
        summary.add_row(escape(fn), escape(cx), escape(st))
    if len(inv) > 40:
        summary.add_row(f"... ({len(inv) - 40} more)", "-", "-")

    waves_n = len(plan.waves)
    notes_n = len(plan.notes)
    n_merge = len((report.get("alias_merge") or {}).get("merged_entities") or [])
    panel = Panel(
        summary,
        title="[bold]Run complete[/bold]",
        subtitle=(
            f"[dim]{waves_n} wave(s); {notes_n} plan note(s); "
            f"{n_merge} merged entity row(s); report -> {escape(str(out_report))}[/dim]"
        ),
        border_style="green",
    )
    console.print(panel)

    if not launch_dashboard:
        console.print("\n[dim]Skipping dashboard (--no-dashboard).[/dim]")
        return 0

    dashboard_py = _repo_root() / "src" / "ama" / "ui" / "dashboard.py"
    if not dashboard_py.is_file():
        console.print(f"[yellow]dashboard.py not found at {dashboard_py}[/yellow]")
        return 0

    env = os.environ.copy()
    env["AMA_REPORT_PATH"] = str(out_report.resolve())

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_py),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    console.print("\n[bold]Launching Streamlit[/bold] [dim](subprocess, AMA_REPORT_PATH set)...[/dim]")
    try:
        subprocess.Popen(cmd, env=env, cwd=str(_repo_root()))  # noqa: S603
    except OSError as e:
        console.print(Panel(f"[red]Failed to start Streamlit:[/red] {e}", border_style="red"))
        return 1

    url = "http://localhost:8501"
    time.sleep(2.0)
    try:
        webbrowser.open(url)
    except OSError:
        console.print(f"[yellow]Open manually:[/yellow] {url}")

    console.print(
        f"\n[green]Dashboard starting at[/green] [link={url}]{url}[/link] - Ctrl+C in that terminal stops Streamlit."
    )
    return 0


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    root = _repo_root()
    default_log = root / "sample_data" / "sql_logs" / "sample_file.jsonl"
    default_out = root / "demo_report.json"

    p = argparse.ArgumentParser(
        description="AMA stakeholder demo (full ingest: --discovery-mode --discovery-merge-all)",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=root,
        help="Project / data root (default: repo root)",
    )
    p.add_argument(
        "--report-out",
        type=Path,
        default=default_out,
        help="Write report JSON here (default: demo_report.json)",
    )
    p.add_argument(
        "--sql-logs",
        nargs="+",
        metavar="PATH",
        default=None,
        help="Explicit SQL JSONL files (default: sample_data/sql_logs/sample_file.jsonl)",
    )
    p.add_argument(
        "--all-project-sql-logs",
        action="store_true",
        help="Use **/sql_logs/**/*.jsonl under --data-root (slower; more tables)",
    )
    p.add_argument(
        "--skip-vectors",
        action="store_true",
        help="Pass through to ama-ingest (faster; skips comms/git embedding index)",
    )
    p.add_argument(
        "--discovery-merge-max",
        type=int,
        default=None,
        metavar="N",
        help="Cap DDL merge tables (0 = unlimited; same as ama-ingest --discovery-merge-max)",
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Do not spawn Streamlit or open the browser",
    )
    args = p.parse_args()

    data_root = args.data_root.expanduser().resolve()
    out_report = args.report_out.expanduser().resolve()

    if args.all_project_sql_logs:
        sql_logs_arg: list[str] | None = None
    elif args.sql_logs:
        sql_logs_arg = [str(Path(p).expanduser().resolve()) for p in args.sql_logs]
        for p in sql_logs_arg:
            if not Path(p).is_file():
                Console(stderr=True).print(f"[red]SQL log not found:[/red] {p}")
                return 2
    else:
        if not default_log.is_file():
            Console(stderr=True).print(f"[red]Default SQL log missing:[/red] {default_log}")
            return 2
        sql_logs_arg = [str(default_log)]

    console = Console()
    try:
        return _run_demo(
            data_root=data_root,
            out_report=out_report,
            sql_logs=sql_logs_arg,
            skip_vectors=bool(args.skip_vectors),
            discovery_merge_max=args.discovery_merge_max,
            launch_dashboard=not args.no_dashboard,
            console=console,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130
    except Exception as e:
        Console(stderr=True).print(Panel(str(e), title="[red]Demo failed[/red]", border_style="red"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
