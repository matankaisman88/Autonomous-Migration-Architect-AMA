import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Button,
  Chip,
  Divider,
  Grid2,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { QueueChip, useRequireReportId, useErrorSetter } from "./common";
import { PlannerRationale } from "./planner-format";
import { useAppState } from "../state";
import type { ScoredTable } from "../types";

type Wave = {
  wave_id: number;
  name: string;
  business_rationale: string;
  technical_rationale: string;
  tables: { full_name: string; business_domain: string }[];
};

export function PlannerPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const { setNotice } = useAppState();
  const [expandedWave, setExpandedWave] = useState<number | false>(false);
  const [data, setData] = useState<{ migration_context?: string; notes?: string[]; waves?: Wave[] } | null>(null);
  const [scoredByTable, setScoredByTable] = useState<Record<string, ScoredTable>>({});
  const [tableSearchByWave, setTableSearchByWave] = useState<Record<number, string>>({});
  const [queueFilterByWave, setQueueFilterByWave] = useState<Record<number, string>>({});
  const waves = data?.waves ?? [];
  const totalTables = useMemo(() => waves.reduce((acc, w) => acc + (w.tables?.length ?? 0), 0), [waves]);
  const uniqueDomains = useMemo(
    () =>
      Array.from(
        new Set(
          waves.flatMap((w) => (w.tables ?? []).map((t) => t.business_domain).filter((d) => Boolean(String(d).trim())))
        )
      ),
    [waves]
  );
  const hasScores = Object.keys(scoredByTable).length > 0;

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12, lg: 4 }}>
        <PageCard title="Migration Waves">
          <Stack spacing={1.5}>
            <Alert severity="info" sx={{ py: 0.5 }}>
              Generate a phased execution plan, then review wave rationale and table scope before running.
            </Alert>
            <Button
              variant="contained"
              disabled={!reportId}
              onClick={async () => {
                try {
                  const [plannedRaw, evalRes] = await Promise.all([api.planWaves(reportId), api.evaluate(reportId)]);
                  const planned = plannedRaw as { migration_context?: string; notes?: string[]; waves?: Wave[] };
                  setData(planned);
                  setExpandedWave(planned.waves?.[0]?.wave_id ?? false);
                  const mapping: Record<string, ScoredTable> = {};
                  for (const row of evalRes.scored_tables ?? []) {
                    mapping[row.table_key] = row;
                  }
                  setScoredByTable(mapping);
                  setTableSearchByWave({});
                  setQueueFilterByWave({});
                  setNotice(`Generated ${planned.waves?.length ?? 0} migration wave(s).`);
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Generate Plan
            </Button>
            {!hasScores && data && (
              <Typography variant="caption" color="text.secondary">
                Queue chips appear after plan generation (evaluate runs automatically).
              </Typography>
            )}
            <Divider />
            <Paper variant="outlined" sx={{ p: 1.5 }}>
              <Typography variant="subtitle2">Plan Snapshot</Typography>
              <Typography variant="body2">Waves: {waves.length}</Typography>
              <Typography variant="body2">Tables in plan: {totalTables}</Typography>
              <Typography variant="body2">Domains: {uniqueDomains.length}</Typography>
            </Paper>
            {data && (
              <>
                <Typography variant="body2" color="text.secondary">
                  Context: {data.migration_context || "n/a"}
                </Typography>
                <Stack direction="row" spacing={0.8} flexWrap="wrap" useFlexGap>
                  {uniqueDomains.map((d) => (
                    <Chip key={d} label={d} size="small" />
                  ))}
                </Stack>
              </>
            )}
          </Stack>
        </PageCard>
      </Grid2>

      <Grid2 size={{ xs: 12, lg: 8 }}>
        <PageCard title="Wave Details">
          {data && (
            <Stack spacing={1.5} sx={{ mt: 1 }}>
              {(data.notes ?? []).length > 0 && (
                <Alert severity="info" variant="outlined">
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                    Planner notes
                  </Typography>
                  {(data.notes ?? []).map((note, idx) => (
                    <Typography key={idx} variant="body2" sx={{ lineHeight: 1.5 }}>
                      {idx + 1}. {note}
                    </Typography>
                  ))}
                </Alert>
              )}
              {waves.map((wave) => {
                const tableSearch = tableSearchByWave[wave.wave_id] ?? "";
                const queueFilter = queueFilterByWave[wave.wave_id] ?? "all";
                const waveDomains = Array.from(
                  new Set(wave.tables.map((t) => t.business_domain).filter((d) => Boolean(String(d).trim())))
                );
                const schemas = Array.from(
                  new Set(
                    wave.tables
                      .map((t) => String(t.full_name || "").split(".")[0])
                      .filter((s) => Boolean(String(s).trim()))
                  )
                );
                const queueCounts = wave.tables.reduce(
                  (acc, t) => {
                    const scored = scoredByTable[t.full_name];
                    const q = scored?.queue;
                    if (q === "green") acc.green += 1;
                    else if (q === "yellow") acc.yellow += 1;
                    else if (q === "red") acc.red += 1;
                    else acc.unknown += 1;
                    return acc;
                  },
                  { green: 0, yellow: 0, red: 0, unknown: 0 }
                );
                const filteredTables = wave.tables.filter((t) => {
                  const scored = scoredByTable[t.full_name];
                  if (queueFilter !== "all" && scored?.queue !== queueFilter) return false;
                  if (!tableSearch.trim()) return true;
                  return t.full_name.toLowerCase().includes(tableSearch.trim().toLowerCase());
                });

                return (
                  <Accordion
                    key={wave.wave_id}
                    disableGutters
                    expanded={expandedWave === wave.wave_id}
                    onChange={(_, isExpanded) => setExpandedWave(isExpanded ? wave.wave_id : false)}
                    sx={{ border: "1px solid", borderColor: "divider", borderRadius: "8px !important", "&:before": { display: "none" } }}
                  >
                    <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ px: 2 }}>
                      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                        <Chip label={`Wave ${wave.wave_id}`} size="small" color="primary" />
                        <Typography fontWeight={600}>{wave.name}</Typography>
                        <Chip label={`${wave.tables.length} tables`} size="small" variant="outlined" />
                      </Stack>
                    </AccordionSummary>
                    <AccordionDetails sx={{ px: 2, pb: 2 }}>
                      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap" }} useFlexGap>
                        <Chip size="small" label={`Tables: ${wave.tables.length}`} />
                        <Chip size="small" label={`Schemas: ${schemas.join(", ") || "—"}`} />
                        {waveDomains.length === 1 ? (
                          <Chip size="small" label={`Domain: ${waveDomains[0]}`} />
                        ) : (
                          waveDomains.map((d) => <Chip key={d} size="small" label={d} variant="outlined" />)
                        )}
                        <Chip size="small" color="success" variant="outlined" label={`Green ${queueCounts.green}`} />
                        <Chip size="small" color="warning" variant="outlined" label={`Yellow ${queueCounts.yellow}`} />
                        <Chip size="small" color="error" variant="outlined" label={`Red ${queueCounts.red}`} />
                      </Stack>

                      <Grid2 container spacing={2} sx={{ mb: 2 }}>
                        <Grid2 size={{ xs: 12, lg: 6 }}>
                          <PlannerRationale title="Business rationale" text={wave.business_rationale} />
                        </Grid2>
                        <Grid2 size={{ xs: 12, lg: 6 }}>
                          <PlannerRationale title="Technical rationale" text={wave.technical_rationale} />
                        </Grid2>
                      </Grid2>

                      <Typography variant="subtitle2" sx={{ mb: 1 }}>
                        Tables in this wave
                      </Typography>
                      <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1.5 }} useFlexGap>
                        <TextField
                          size="small"
                          label="Search table"
                          value={tableSearch}
                          onChange={(e) =>
                            setTableSearchByWave((prev) => ({ ...prev, [wave.wave_id]: e.target.value }))
                          }
                          sx={{ flex: 1, minWidth: 180 }}
                        />
                        <TextField
                          select
                          size="small"
                          label="Queue filter"
                          value={queueFilter}
                          onChange={(e) =>
                            setQueueFilterByWave((prev) => ({ ...prev, [wave.wave_id]: e.target.value }))
                          }
                          sx={{ minWidth: 150 }}
                        >
                          <MenuItem value="all">All queues</MenuItem>
                          <MenuItem value="green">Green only</MenuItem>
                          <MenuItem value="yellow">Yellow only</MenuItem>
                          <MenuItem value="red">Red only</MenuItem>
                        </TextField>
                      </Stack>

                      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 320 }}>
                        <Table size="small" stickyHeader>
                          <TableHead>
                            <TableRow>
                              <TableCell width={48}>#</TableCell>
                              <TableCell>Table</TableCell>
                              <TableCell width={100}>Schema</TableCell>
                              <TableCell width={120}>Queue</TableCell>
                              <TableCell align="right" width={90}>
                                Confidence
                              </TableCell>
                              <TableCell align="right" width={90}>
                                Criticality
                              </TableCell>
                              {waveDomains.length > 1 && <TableCell>Domain</TableCell>}
                            </TableRow>
                          </TableHead>
                          <TableBody>
                            {filteredTables.length === 0 ? (
                              <TableRow>
                                <TableCell colSpan={waveDomains.length > 1 ? 7 : 6}>
                                  <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
                                    No tables match the current filters.
                                  </Typography>
                                </TableCell>
                              </TableRow>
                            ) : (
                              filteredTables.map((t, idx) => {
                                const scored = scoredByTable[t.full_name];
                                const schema = String(t.full_name || "").split(".")[0] || "—";
                                return (
                                  <TableRow key={t.full_name} hover>
                                    <TableCell>{idx + 1}</TableCell>
                                    <TableCell>
                                      <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.82rem" }}>
                                        {t.full_name}
                                      </Typography>
                                    </TableCell>
                                    <TableCell>{schema}</TableCell>
                                    <TableCell>
                                      {scored?.queue ? <QueueChip queue={scored.queue} /> : "—"}
                                    </TableCell>
                                    <TableCell align="right">{scored?.confidence ?? "—"}</TableCell>
                                    <TableCell align="right">{scored?.criticality ?? "—"}</TableCell>
                                    {waveDomains.length > 1 && <TableCell>{t.business_domain || "—"}</TableCell>}
                                  </TableRow>
                                );
                              })
                            )}
                          </TableBody>
                        </Table>
                      </TableContainer>
                    </AccordionDetails>
                  </Accordion>
                );
              })}
            </Stack>
          )}
          {!data && (
            <Typography variant="body2" color="text.secondary">
              Generate a plan to view wave breakdown, rationale, and per-wave table inventory.
            </Typography>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}
