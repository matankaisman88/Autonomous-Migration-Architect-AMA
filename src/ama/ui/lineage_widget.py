"""
Interactive table lineage via Pyvis (optional dependency).

Install: ``pip install pyvis`` or ``pip install -e ".[viz]"``.
"""

from __future__ import annotations

from typing import Any

# Shown in the Streamlit UI when pyvis is not installed.
PYVIS_INSTALL_HINT = (
    "Interactive lineage requires **pyvis**. Install with: "
    "`pip install pyvis` or `pip install -e \".[viz]\"` from the project root."
)


def pyvis_available() -> bool:
    try:
        import pyvis  # noqa: F401
    except ImportError:
        return False
    return True


def lineage_subgraph_html(
    lineage_data: dict[str, Any] | None,
    target_table: str,
    *,
    height_px: int = 440,
) -> str | None:
    """
    Build an interactive HTML graph (Pyvis) for the 1-hop neighborhood of ``target_table``.

    Parameters
    ----------
    lineage_data
        The ``report["lineage"]`` object (expects ``edges``: list of ``from`` / ``to`` / ``weight``).
    target_table
        Fully qualified table name to center the subgraph on.

    Returns
    -------
    Complete HTML document string for ``st.components.v1.html``, or ``None`` if there is nothing
    to draw or Pyvis is not installed.
    """
    if not str(target_table).strip():
        return None
    lineage = lineage_data or {}
    edges = lineage.get("edges") or []
    if not edges:
        return None
    try:
        from pyvis.network import Network
    except ImportError:
        return None

    center = target_table.strip()
    nodes: set[str] = {center}
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a == center or b == center:
            if a:
                nodes.add(a)
            if b:
                nodes.add(b)

    net = Network(
        height=f"{height_px}px",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="#eaeaea",
        cdn_resources="remote",
    )
    net.set_options(
        '{"physics": {"enabled": true, "solver": "forceAtlas2Based", '
        '"forceAtlas2Based": {"gravitationalConstant": -38}}, '
        '"edges": {"smooth": {"type": "continuous"}}}'
    )
    for nd in sorted(nodes):
        net.add_node(
            nd,
            label=nd[:56] + ("…" if len(nd) > 56 else ""),
            color="#e94560" if nd == center else "#16213e",
            font={"color": "#eaeaea"},
        )

    seen: set[tuple[str, str]] = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a not in nodes or b not in nodes or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        w = int(e.get("weight") or 1)
        net.add_edge(a, b, value=w, title=f"weight {w}")

    return net.generate_html()


def lineage_subgraph_html_from_report(
    report: dict[str, Any],
    target_table: str,
    *,
    height_px: int = 440,
) -> str | None:
    """Convenience wrapper: ``lineage_subgraph_html(report.get(\"lineage\"), target_table)``."""
    return lineage_subgraph_html(report.get("lineage") if isinstance(report, dict) else None, target_table, height_px=height_px)
