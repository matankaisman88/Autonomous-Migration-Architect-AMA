"""
Interactive table lineage via Pyvis (optional dependency).

Install: ``pip install pyvis`` or ``pip install -e ".[viz]"``.
"""

from __future__ import annotations

import json
import math
import re
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


def _label_for_node(full_id: str, *, max_len: int = 48) -> str:
    """Readable label: prefer ``schema`` + line break + ``table`` when name is long."""
    s = str(full_id).strip()
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    parts = s.rsplit(".", 1)
    if len(parts) == 2 and len(parts[0]) + len(parts[1]) + 1 > max_len:
        a, b = parts[0], parts[1]
        if len(b) > 36:
            b = b[:35] + "…"
        return f"{a}\n{b}"
    return s[: max_len - 1] + "…"


def _radial_xy(center: str, nodes: set[str]) -> dict[str, tuple[float, float]]:
    """Place center at origin; neighbors on a circle (readability without physics drift)."""
    c = center.strip()
    others = sorted(n for n in nodes if n != c)
    out: dict[str, tuple[float, float]] = {c: (0.0, 0.0)}
    n_other = len(others)
    if n_other == 0:
        return out
    # Radius grows with neighbor count so labels do not overlap badly.
    r = max(130.0, min(55.0 * math.sqrt(float(n_other)), 420.0))
    for i, nd in enumerate(others):
        ang = 2.0 * math.pi * (i / n_other) - (math.pi / 2.0)
        out[nd] = (r * math.cos(ang), r * math.sin(ang))
    return out


def _inject_viewport_fit(html: str) -> str:
    """
    vis-network defaults often leave a small graph in a corner of the iframe.
    Fit after the first frame so the container has real dimensions (Streamlit).
    """
    return re.sub(
        r"drawGraph\(\);\s*</script>",
        """drawGraph();
              (function () {
                function fitView() {
                  try {
                    if (typeof network !== "undefined" && network.fit) {
                      network.fit({ animation: false, padding: 56 });
                    }
                  } catch (e) {}
                }
                fitView();
                requestAnimationFrame(function () {
                  requestAnimationFrame(fitView);
                });
                setTimeout(fitView, 50);
                setTimeout(fitView, 250);
              })();
        </script>""",
        html,
        count=1,
    )


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
    # Fixed radial layout + disabled physics: default force layout drifts and sits in a corner
    # inside Streamlit iframes; users had to pan/zoom before the graph was interpretable.
    net.set_options(
        json.dumps(
            {
                "physics": {"enabled": False},
                "interaction": {
                    "hover": True,
                    "tooltipDelay": 80,
                    "zoomView": True,
                    "dragView": True,
                },
                "edges": {
                    "smooth": {"type": "continuous", "roundness": 0.35},
                },
                "nodes": {
                    "font": {"size": 14, "color": "#eaeaea"},
                    "borderWidth": 2,
                },
            }
        )
    )

    xy = _radial_xy(center, nodes)
    for nd in nodes:
        x, y = xy[nd]
        is_center = nd == center
        net.add_node(
            nd,
            label=_label_for_node(nd),
            title=nd,
            x=x,
            y=y,
            fixed={"x": True, "y": True},
            physics=False,
            shape="dot",
            size=26 if is_center else 20,
            color=(
                {"background": "#e94560", "border": "#ff7b8a", "highlight": {"background": "#ff5c78", "border": "#fff"}}
                if is_center
                else {"background": "#16213e", "border": "#0f3460", "highlight": {"background": "#1f4068", "border": "#eaeaea"}}
            ),
            font={"color": "#eaeaea", "size": 13 if is_center else 12},
        )

    pair_w: dict[tuple[str, str], int] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a not in nodes or b not in nodes or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        w = int(e.get("weight") or 1)
        pair_w[key] = max(pair_w.get(key, 0), w)

    weights = list(pair_w.values())
    lo = min(weights) if weights else 1
    hi = max(weights) if weights else 1

    def _width_for(w: int) -> float:
        if hi <= lo:
            return 5.0
        return 2.5 + 7.5 * (float(w - lo) / float(hi - lo))

    for (a, b), w in sorted(pair_w.items()):
        net.add_edge(
            a,
            b,
            value=w,
            title=f"Co-query weight: {w}",
            width=_width_for(w),
            color={"color": "#9aa8c4", "highlight": "#ffffff"},
        )

    return _inject_viewport_fit(net.generate_html())


def lineage_subgraph_html_from_report(
    report: dict[str, Any],
    target_table: str,
    *,
    height_px: int = 440,
) -> str | None:
    """Convenience wrapper: ``lineage_subgraph_html(report.get(\"lineage\"), target_table)``."""
    return lineage_subgraph_html(report.get("lineage") if isinstance(report, dict) else None, target_table, height_px=height_px)
