from __future__ import annotations

from pathlib import Path

from ama.reports import (
    default_report_filename,
    resolve_report_output_path,
    write_report_file,
)


def test_resolve_empty_spec_uses_default_name(tmp_path: Path) -> None:
    p = resolve_report_output_path(
        "",
        table_full_name="sales.orders",
        extension=".md",
        cwd=tmp_path,
    )
    assert p.parent == tmp_path.resolve()
    assert p.name.startswith("ama_report_sales_orders_")
    assert p.suffix == ".md"


def test_resolve_existing_directory(tmp_path: Path) -> None:
    d = tmp_path / "exports"
    d.mkdir()
    p = resolve_report_output_path(
        str(d),
        table_full_name="sales.orders",
        extension=".json",
        cwd=tmp_path,
    )
    assert p.parent == d.resolve()
    assert p.suffix == ".json"
    assert "ama_report_sales_orders_" in p.name


def test_resolve_file_without_suffix_gets_extension(tmp_path: Path) -> None:
    p = resolve_report_output_path(
        str(tmp_path / "out"),
        table_full_name="sales.orders",
        extension=".md",
        cwd=tmp_path,
    )
    assert p.suffix == ".md"
    assert p.name == "out.md"


def test_write_report_file_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "x.md"
    out = write_report_file(target, "# hi\n")
    assert out == target.resolve()
    assert target.read_text(encoding="utf-8") == "# hi\n"


def test_default_report_filename_shape() -> None:
    name = default_report_filename("sales.orders", ".md")
    assert name.startswith("ama_report_sales_orders_")
    assert name.endswith(".md")
