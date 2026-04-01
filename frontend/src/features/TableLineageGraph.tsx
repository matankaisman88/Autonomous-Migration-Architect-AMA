import "@xyflow/react/dist/style.css";

import { Box, CircularProgress, Typography } from "@mui/material";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes
} from "@xyflow/react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { LineageFlowEdge, LineageFlowNode, LineageNodeRole } from "../types";

function roleColors(role: LineageNodeRole): { bg: string; border: string; color: string } {
  switch (role) {
    case "center":
      return { bg: "#be123c", border: "#fda4af", color: "#fff7ed" };
    case "broken":
      return { bg: "#991b1b", border: "#f87171", color: "#fef2f2" };
    default:
      return { bg: "#1e3a5f", border: "#38bdf8", color: "#e2e8f0" };
  }
}

function LineageTableNode({ data }: NodeProps) {
  const role = (data.role as LineageNodeRole) || "neighbor";
  const c = roleColors(role);
  const label = String(data.label ?? "");
  return (
    <Box
      sx={{
        px: 1.25,
        py: 0.75,
        borderRadius: 1,
        minWidth: 100,
        maxWidth: 220,
        bgcolor: c.bg,
        border: `2px solid ${c.border}`,
        color: c.color,
        fontSize: 12,
        fontWeight: 600,
        textAlign: "center",
        wordBreak: "break-word"
      }}
    >
      <Handle type="target" position={Position.Left} id="l" style={{ background: c.border }} />
      {label}
      <Handle type="source" position={Position.Right} id="r" style={{ background: c.border }} />
    </Box>
  );
}

const nodeTypes: NodeTypes = { lineageTable: LineageTableNode };

function FlowInner({
  reportId,
  tableKey,
  onError
}: {
  reportId: string;
  tableKey: string;
  onError: (msg: string) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [loading, setLoading] = useState(true);
  const [hint, setHint] = useState("");
  const { fitView } = useReactFlow();

  const runFit = useCallback(() => {
    requestAnimationFrame(() => {
      fitView({ padding: 0.25, duration: 200 });
    });
  }, [fitView]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setHint("");
    (async () => {
      try {
        const res = await api.getLineage(reportId, tableKey);
        if (cancelled) return;
        if (res.empty_reason) {
          const msg =
            res.empty_reason === "no_lineage_edges"
              ? "No lineage edges in this report (run ingestion with discovery mode)."
              : res.empty_reason === "no_edges_for_table"
                ? "No co-query neighbors found for this table."
                : res.empty_reason;
          setHint(msg);
        } else {
          setHint("");
        }
        const n: Node[] = res.nodes.map((x: LineageFlowNode) => ({
          id: x.id,
          type: x.type || "lineageTable",
          position: x.position,
          data: { ...x.data }
        }));
        const e: Edge[] = res.edges.map((x: LineageFlowEdge) => ({
          id: x.id,
          source: x.source,
          target: x.target,
          label: x.label,
          data: x.data,
          style: { stroke: "#64748b", strokeWidth: 1.5 },
          labelStyle: { fill: "#94a3b8", fontSize: 10 },
          labelBgStyle: { fill: "#0f172a" }
        }));
        setNodes(n);
        setEdges(e);
        runFit();
      } catch (err) {
        if (!cancelled) {
          onError(err instanceof Error ? err.message : String(err));
          setNodes([]);
          setEdges([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reportId, tableKey, setNodes, setEdges, onError, runFit]);

  useEffect(() => {
    if (nodes.length > 0) runFit();
  }, [nodes.length, runFit, nodes]);

  if (loading) {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 4, justifyContent: "center" }}>
        <CircularProgress size={22} />
        <Typography variant="body2" color="text.secondary">
          Loading lineage…
        </Typography>
      </Box>
    );
  }

  if (nodes.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        {hint || "No lineage graph to display."}
      </Typography>
    );
  }

  return (
    <Box sx={{ height: 420, width: "100%", borderRadius: 1, overflow: "hidden", border: "1px solid", borderColor: "divider" }}>
      {hint ? (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: 1, py: 0.5 }}>
          {hint}
        </Typography>
      ) : null}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </Box>
  );
}

export function TableLineageGraph({
  reportId,
  tableKey,
  onError
}: {
  reportId: string;
  tableKey: string;
  onError: (msg: string) => void;
}) {
  return (
    <ReactFlowProvider>
      <FlowInner reportId={reportId} tableKey={tableKey} onError={onError} />
    </ReactFlowProvider>
  );
}
