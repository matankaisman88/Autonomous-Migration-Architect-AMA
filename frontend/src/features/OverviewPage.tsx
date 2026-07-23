import {
  Alert,
  Button,
  Chip,
  Divider,
  Grid2,
  Link,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableContainer,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography
} from "@mui/material";
import InsightsIcon from "@mui/icons-material/Insights";
import TableChartIcon from "@mui/icons-material/TableChart";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { StatCard } from "../components/StatCard";
import { useRequireReportId, useErrorSetter } from "./common";
import { useAppState } from "../state";
import type { ReportSummary } from "../types";
import { Link as RouterLink } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type ImpactRow = {
  label: string;
  source_table: string;
  ddl: string;
  confidence: number;
  importance: number;
  query_volume: number;
};

export function OverviewPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const { setSummary } = useAppState();
  const [summaryRaw, setSummaryRaw] = useState<ReportSummary | null>(null);
  const [impactRows, setImpactRows] = useState<ImpactRow[]>([]);
  const [impactSearch, setImpactSearch] = useState("");
  const [minConfidence, setMinConfidence] = useState(0);
  const [minImportance, setMinImportance] = useState(0);
  const [rowLimit, setRowLimit] = useState(10);

  const shownImpactRows = useMemo(
    () =>
      impactRows
        .filter((r) => r.confidence >= minConfidence)
        .filter((r) => r.importance >= minImportance)
        .filter((r) => {
          const q = impactSearch.trim().toLowerCase();
          if (!q) return true;
          return (
            String(r.source_table || "").toLowerCase().includes(q) ||
            String(r.ddl || "").toLowerCase().includes(q)
          );
        })
        .slice(0, rowLimit),
    [impactRows, minConfidence, minImportance, impactSearch, rowLimit]
  );

  useEffect(() => {
    if (!reportId) return;
    (async () => {
      try {
        const [summary, impact] = await Promise.all([api.getSummary(reportId), api.impactScatter(reportId)]);
        setSummary(summary);
        setSummaryRaw(summary);
        setImpactRows(Array.isArray(impact.rows) ? (impact.rows as ImpactRow[]) : []);
      } catch (e) {
        setError(e);
      }
    })();
  }, [reportId, setSummary, setError]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12 }}>
        <Alert severity="info" variant="outlined" sx={{ borderRadius: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 0.75 }}>
            Lineage &amp; live database (new)
          </Typography>
          <Typography variant="body2" component="div">
            <strong>Lineage graph</strong> — open{" "}
            <Link component={RouterLink} to="/tables" underline="hover" fontWeight={600}>
              Tables
            </Link>
            , load a report in the header, click <strong>Evaluate</strong>, then pick a table. The React Flow co-query graph is in the full-width{" "}
            <em>Table lineage</em> card below the grid.
          </Typography>
          <Typography variant="body2" component="div" sx={{ mt: 1 }}>
            <strong>Live connection</strong> — use the left nav{" "}
            <Link component={RouterLink} to="/live" underline="hover" fontWeight={600}>
              Live connection
            </Link>{" "}
            for read-only SQL Server extraction, WebSocket progress, <code>live_data/</code> exports, and optional{" "}
            <strong>build report / auto-open Tables</strong>. If that page looks unchanged, rebuild the Docker{" "}
            <code>web</code> image or run the Vite dev server from <code>frontend/</code>.
          </Typography>
        </Alert>
      </Grid2>
      <Grid2 size={{ xs: 12, md: 3 }}>
        <StatCard
          label="Tables"
          value={summaryRaw?.table_count ?? "-"}
          icon={<TableChartIcon color="primary" />}
          subtitle="discovered inventory"
        />
      </Grid2>
      <Grid2 size={{ xs: 12, md: 3 }}>
        <StatCard
          label="Domains"
          value={summaryRaw?.domains?.length ?? "-"}
          icon={<InsightsIcon color="primary" />}
          subtitle="business segmentation"
        />
      </Grid2>
      <Grid2 size={{ xs: 12, md: 6 }}>
        <PageCard title="Summary">
          <Stack spacing={1}>
            <Typography variant="body2">Report ID: {reportId || "-"}</Typography>
            <Button
              variant="outlined"
              disabled={!reportId}
              onClick={async () => {
                const s = await api.getSummary(reportId);
                setSummaryRaw(s);
                setSummary(s);
              }}
            >
              Refresh Summary
            </Button>
          </Stack>
          {summaryRaw && (
            <Stack spacing={1.2} sx={{ mt: 2 }}>
              <Paper variant="outlined" sx={{ p: 1.5 }}>
                <Typography variant="body2">Tables: {summaryRaw.table_count}</Typography>
                <Typography variant="body2">Lineage edges: {summaryRaw.lineage_edge_count}</Typography>
                <Typography variant="body2">
                  Glossary: <strong>{summaryRaw.has_glossary ? "Yes" : "No"}</strong>
                </Typography>
              </Paper>
              <Divider />
              <Typography variant="subtitle2">Domains</Typography>
              <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap>
                {summaryRaw.domains.map((d) => (
                  <Chip key={d} label={d} size="small" />
                ))}
              </Stack>
              <Typography variant="caption" color="text.secondary">
                Context: {summaryRaw.migration_context || "n/a"}
              </Typography>
            </Stack>
          )}
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12 }}>
        <PageCard title="Impact Readiness">
          <Stack direction="row" spacing={1}>
            <Button
              variant="contained"
              disabled={!reportId}
              onClick={async () => {
                const impact = await api.impactScatter(reportId);
                setImpactRows(Array.isArray(impact.rows) ? (impact.rows as ImpactRow[]) : []);
              }}
            >
              Refresh Impact Data
            </Button>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mt: 1 }} useFlexGap flexWrap="wrap">
            <TextField
              size="small"
              label="Search table/column"
              value={impactSearch}
              onChange={(e) => setImpactSearch(e.target.value)}
              fullWidth
            />
            <TextField
              size="small"
              label="Min confidence"
              type="number"
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value || 0))}
            />
            <TextField
              size="small"
              label="Min importance"
              type="number"
              value={minImportance}
              onChange={(e) => setMinImportance(Number(e.target.value || 0))}
            />
            <TextField
              select
              size="small"
              label="Rows"
              value={rowLimit}
              onChange={(e) => setRowLimit(Number(e.target.value))}
              sx={{ minWidth: 110 }}
            >
              <MenuItem value={10}>10</MenuItem>
              <MenuItem value={25}>25</MenuItem>
              <MenuItem value={50}>50</MenuItem>
            </TextField>
          </Stack>
          <Grid2 container spacing={2} sx={{ mt: 0.5 }}>
            <Grid2 size={{ xs: 12, xl: 4 }} sx={{ height: 280, minWidth: 0 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={shownImpactRows} margin={{ top: 8, right: 12, left: 12, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="ddl" hide />
                  <YAxis />
                  <Tooltip />
                  <Bar dataKey="confidence" fill="#0ea5e9" />
                </BarChart>
              </ResponsiveContainer>
            </Grid2>
            <Grid2 size={{ xs: 12, xl: 8 }} sx={{ minWidth: 0 }}>
              <TableContainer sx={{ mt: 1, maxHeight: 320, overflowX: "hidden", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                <Table
                  size="small"
                  stickyHeader
                  sx={{
                    width: "100%",
                    tableLayout: "fixed",
                    "& .MuiTableCell-root": {
                      px: 1,
                      py: 0.8,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis"
                    }
                  }}
                >
                  <TableHead>
                    <TableRow>
                      <TableCell sx={{ width: "42%" }}>Table</TableCell>
                      <TableCell sx={{ width: "20%" }}>Column</TableCell>
                      <TableCell align="right">Confidence</TableCell>
                      <TableCell align="right">Importance</TableCell>
                      <TableCell align="right" sx={{ width: "15%" }}>Query Volume</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {shownImpactRows.map((row) => (
                      <TableRow key={row.label}>
                        <TableCell>{row.source_table}</TableCell>
                        <TableCell>{row.ddl}</TableCell>
                        <TableCell align="right">{row.confidence?.toFixed?.(2) ?? row.confidence}</TableCell>
                        <TableCell align="right">{row.importance?.toFixed?.(2) ?? row.importance}</TableCell>
                        <TableCell align="right">{Math.round(row.query_volume ?? 0)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            </Grid2>
          </Grid2>
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

