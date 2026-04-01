import {
  Button,
  Box,
  CircularProgress,
  Chip,
  Divider,
  Grid2,
  Checkbox,
  FormControlLabel,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography
} from "@mui/material";
import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { TableLineageGraph } from "./TableLineageGraph";
import { QueueChip, useRequireReportId, useErrorSetter } from "./common";
import type { ScoredTable } from "../types";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import { useAppState } from "../state";
import { useNavigate } from "react-router-dom";

export function TablesPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const { setNotice } = useAppState();
  const navigate = useNavigate();
  const [rows, setRows] = useState<ScoredTable[]>([]);
  const [selectedTable, setSelectedTable] = useState("");
  const [explain, setExplain] = useState<Record<string, unknown> | null>(null);
  const [proposal, setProposal] = useState<Record<string, unknown> | null>(null);
  const [approveResult, setApproveResult] = useState<Record<string, unknown> | null>(null);
  const [isProposing, setIsProposing] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const [domainFilter, setDomainFilter] = useState<string>("all");
  const [queueFilter, setQueueFilter] = useState<string[]>(["green", "yellow", "red"]);
  const [search, setSearch] = useState("");
  const [minConfidence, setMinConfidence] = useState(0);
  const [maxCriticality, setMaxCriticality] = useState(100);
  const [sortBy, setSortBy] = useState<"name" | "confidence" | "criticality">("name");
  const [migratedTables, setMigratedTables] = useState<string[]>([]);
  const [hideMigrated, setHideMigrated] = useState(true);
  const greenCount = useMemo(() => rows.filter((r) => r.queue === "green").length, [rows]);
  const yellowCount = useMemo(() => rows.filter((r) => r.queue === "yellow").length, [rows]);
  const redCount = useMemo(() => rows.filter((r) => r.queue === "red").length, [rows]);
  const filteredRows = useMemo(
    () =>
      rows
        .filter((r) => (domainFilter === "all" ? true : r.business_domain === domainFilter))
        .filter((r) => queueFilter.includes(r.queue))
        .filter((r) => (hideMigrated ? !migratedTables.includes(r.table_key) : true))
        .filter((r) => r.confidence >= minConfidence)
        .filter((r) => r.criticality <= maxCriticality)
        .filter((r) => {
          const q = search.trim().toLowerCase();
          if (!q) return true;
          return (
            r.table_key.toLowerCase().includes(q) ||
            r.business_domain.toLowerCase().includes(q) ||
            r.queue.toLowerCase().includes(q)
          );
        })
        .sort((a, b) => {
          if (sortBy === "confidence") return b.confidence - a.confidence;
          if (sortBy === "criticality") return b.criticality - a.criticality;
          return a.table_key.localeCompare(b.table_key);
        }),
    [rows, domainFilter, queueFilter, minConfidence, maxCriticality, search, sortBy, hideMigrated, migratedTables]
  );
  const domains = useMemo(() => ["all", ...Array.from(new Set(rows.map((r) => r.business_domain)))], [rows]);
  const columns = useMemo<GridColDef<ScoredTable>[]>(
    () => [
      { field: "table_key", headerName: "Table", flex: 1.8, minWidth: 170 },
      { field: "business_domain", headerName: "Domain", flex: 1.1, minWidth: 110 },
      { field: "queue", headerName: "Queue", flex: 0.8, minWidth: 90 },
      { field: "confidence", headerName: "Confidence", flex: 0.8, minWidth: 90 },
      { field: "criticality", headerName: "Criticality", flex: 0.8, minWidth: 90 }
    ],
    []
  );

  async function evaluate() {
    try {
      const ev = await api.evaluate(reportId);
      setRows(ev.scored_tables);
      setSelectedTable(ev.scored_tables[0]?.table_key ?? "");
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Grid2 container spacing={2} alignItems="flex-start">
      <Grid2 size={{ xs: 12, lg: 8 }}>
        <PageCard title="Scored Inventory">
          <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
            <Button variant="contained" disabled={!reportId} onClick={evaluate}>
              Evaluate
            </Button>
            <QueueChip queue="green" />
            <Typography variant="caption">{greenCount}</Typography>
            <QueueChip queue="yellow" />
            <Typography variant="caption">{yellowCount}</Typography>
            <QueueChip queue="red" />
            <Typography variant="caption">{redCount}</Typography>
          </Stack>
          {greenCount > 0 && (
            <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
              <Chip label={`${greenCount} tables ready for bulk`} color="success" />
              <Button size="small" variant="outlined" onClick={() => navigate("/bulk")}>
                Go to Bulk Migration
              </Button>
            </Stack>
          )}
          <TextField select fullWidth label="Domain filter" value={domainFilter} size="small" onChange={(e) => setDomainFilter(e.target.value)}>
            {domains.map((d) => (
              <MenuItem key={d} value={d}>
                {d}
              </MenuItem>
            ))}
          </TextField>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mt: 1 }}>
            <TextField
              label="Search tables / schemas"
              size="small"
              fullWidth
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <TextField
              select
              label="Sort"
              size="small"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as "name" | "confidence" | "criticality")}
              sx={{ minWidth: 190 }}
            >
              <MenuItem value="name">Table name (A-Z)</MenuItem>
              <MenuItem value="confidence">Confidence (high first)</MenuItem>
              <MenuItem value="criticality">Criticality (high first)</MenuItem>
            </TextField>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mt: 1 }}>
            <TextField
              label="Min confidence"
              type="number"
              size="small"
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value || 0))}
            />
            <TextField
              label="Max criticality"
              type="number"
              size="small"
              value={maxCriticality}
              onChange={(e) => setMaxCriticality(Number(e.target.value || 100))}
            />
            <Stack direction="row" spacing={0.5} alignItems="center">
              <FormControlLabel
                control={<Checkbox checked={hideMigrated} onChange={(e) => setHideMigrated(e.target.checked)} />}
                label="Hide migrated"
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={queueFilter.includes("green")}
                    onChange={(e) =>
                      setQueueFilter((prev) =>
                        e.target.checked ? Array.from(new Set([...prev, "green"])) : prev.filter((q) => q !== "green")
                      )
                    }
                  />
                }
                label="Green"
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={queueFilter.includes("yellow")}
                    onChange={(e) =>
                      setQueueFilter((prev) =>
                        e.target.checked ? Array.from(new Set([...prev, "yellow"])) : prev.filter((q) => q !== "yellow")
                      )
                    }
                  />
                }
                label="Yellow"
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={queueFilter.includes("red")}
                    onChange={(e) =>
                      setQueueFilter((prev) =>
                        e.target.checked ? Array.from(new Set([...prev, "red"])) : prev.filter((q) => q !== "red")
                      )
                    }
                  />
                }
                label="Red"
              />
            </Stack>
          </Stack>
          <TextField
            select
            fullWidth
            label="Select table"
            value={selectedTable}
            onChange={(e) => setSelectedTable(e.target.value)}
            size="small"
          >
            {filteredRows.map((r) => (
              <MenuItem key={r.table_key} value={r.table_key}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <QueueChip queue={r.queue} />
                  <span>{r.table_key}</span>
                </Stack>
              </MenuItem>
            ))}
          </TextField>
          <div style={{ height: 300, width: "100%", marginTop: 10 }}>
            <DataGrid
              rows={filteredRows.map((r) => ({ ...r, id: r.table_key }))}
              columns={columns as GridColDef[]}
              disableRowSelectionOnClick
              onRowClick={(params) => setSelectedTable(String(params.row.table_key))}
              pageSizeOptions={[5, 10, 25]}
              initialState={{ pagination: { paginationModel: { pageSize: 10, page: 0 } } }}
              sx={{
                "& .MuiDataGrid-cell, & .MuiDataGrid-columnHeaderTitle": {
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis"
                },
                "& .MuiDataGrid-cell": { py: 0.5 }
              }}
            />
          </div>
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12, lg: 4 }}>
        <PageCard
          title="Table Insights"
          action={
            <Stack direction="row" spacing={1}>
              <Button
                variant="outlined"
                disabled={!selectedTable || !reportId}
                onClick={async () => {
                  try {
                    setExplain(await api.explain(reportId, selectedTable));
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                Explain
              </Button>
              <Button
                variant="contained"
                disabled={!selectedTable || !reportId || isProposing}
                onClick={async () => {
                  try {
                    setIsProposing(true);
                    setProposal(await api.propose(reportId, selectedTable));
                    setApproveResult(null);
                    setShowSql(false);
                  } catch (e) {
                    setError(e);
                  } finally {
                    setIsProposing(false);
                  }
                }}
              >
                {isProposing ? "Generating..." : "Propose SQL"}
              </Button>
              <Button
                variant="contained"
                color="success"
                disabled={!proposal || !selectedTable || !reportId}
                onClick={async () => {
                  try {
                    const modelName = String(proposal?.model_name ?? "").trim();
                    const sql = String(proposal?.sql ?? "");
                    const schemaYml = String(proposal?.schema_yml ?? "");
                    if (!modelName || !sql) {
                      throw new Error("Generate proposal first (missing model/sql).");
                    }
                    const res = await api.approve(reportId, {
                      model_name: modelName,
                      sql,
                      schema_yml: schemaYml,
                      table_key: selectedTable,
                      approved_by: "react-ui"
                    });
                    setApproveResult(res);
                    if (Boolean(res.success)) {
                      setMigratedTables((prev) => (prev.includes(selectedTable) ? prev : [...prev, selectedTable]));
                    }
                    setNotice(`Migration completed for ${selectedTable}`);
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                Approve & Migrate
              </Button>
            </Stack>
          }
        >
          {explain && (
            <>
              <Typography variant="subtitle2">Score Explanation</Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Metric</TableCell>
                    <TableCell>Value</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  <TableRow>
                    <TableCell>Queue</TableCell>
                    <TableCell>{String(explain.queue ?? "")}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>Confidence</TableCell>
                    <TableCell>{String((explain.confidence as { score?: number } | undefined)?.score ?? "")}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>Criticality</TableCell>
                    <TableCell>{String((explain.criticality as { score?: number } | undefined)?.score ?? "")}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>Summary</TableCell>
                    <TableCell>{String(explain.summary ?? "")}</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </>
          )}
          {proposal && (
            <>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle2">Proposed Model</Typography>
              <Typography variant="body2">Name: {String(proposal.model_name ?? "")}</Typography>
              <Typography variant="body2">Confidence: {String(proposal.generation_confidence ?? "")}</Typography>
              <Typography variant="body2">
                Response: {String(proposal.response_ms ?? "-")} ms {proposal.cached ? "(cached)" : ""}
              </Typography>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
                <Typography variant="body2">SQL preview</Typography>
                <Button size="small" variant="outlined" onClick={() => setShowSql((v) => !v)}>
                  {showSql ? "Hide SQL" : "Show SQL"}
                </Button>
              </Stack>
              {showSql ? (
                <Box
                  component="pre"
                  sx={{
                    mt: 0.5,
                    p: 1.2,
                    borderRadius: 1.2,
                    overflow: "auto",
                    maxHeight: 180,
                    bgcolor: "#0f172a",
                    color: "#e2e8f0",
                    fontSize: 12
                  }}
                >
                  {String(proposal.sql ?? "")}
                </Box>
              ) : null}
            </>
          )}
          {isProposing && (
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 2 }}>
              <CircularProgress size={18} />
              <Typography variant="body2" color="text.secondary">
                Generating SQL proposal for `{selectedTable}`...
              </Typography>
            </Stack>
          )}
          {approveResult && (
            <>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle2">Migration Result</Typography>
              <Typography variant="body2">Success: {String(approveResult.success ?? "")}</Typography>
              <Typography variant="body2">Test Passed: {String(approveResult.test_passed ?? "")}</Typography>
              <Typography variant="body2">Path: {String(approveResult.sql_path ?? "-")}</Typography>
              {approveResult.error ? <Typography color="error">Error: {String(approveResult.error)}</Typography> : null}
            </>
          )}
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12 }}>
        <PageCard title="Table lineage (co-query)">
          {reportId && selectedTable ? (
            <TableLineageGraph
              reportId={reportId}
              tableKey={selectedTable}
              onError={(msg) => setError(msg)}
            />
          ) : (
            <Typography variant="body2" color="text.secondary">
              Load a report and select a table to view lineage.
            </Typography>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

