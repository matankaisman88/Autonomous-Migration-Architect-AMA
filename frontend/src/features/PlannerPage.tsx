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
  TableHead,
  TableRow,
  TextField,
  Typography
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useRequireReportId, useErrorSetter } from "./common";
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
  const [queueFilter, setQueueFilter] = useState("all");
  const [tableSearch, setTableSearch] = useState("");
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
                  const [plannedRaw, evalRes] = await Promise.all([
                    api.planWaves(reportId),
                    api.evaluate(reportId)
                  ]);
                  const planned = plannedRaw as { migration_context?: string; notes?: string[]; waves?: Wave[] };
                  setData(planned);
                  setExpandedWave(planned.waves?.[0]?.wave_id ?? false);
                  const mapping: Record<string, ScoredTable> = {};
                  for (const row of evalRes.scored_tables ?? []) {
                    mapping[row.table_key] = row;
                  }
                  setScoredByTable(mapping);
                  setNotice(`Generated ${planned.waves?.length ?? 0} migration wave(s).`);
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Generate Plan
            </Button>
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
            <Stack spacing={1.5} sx={{ mt: 2 }}>
              <Typography variant="subtitle2">Planner Notes</Typography>
              {(data.notes ?? []).map((note, idx) => (
                <Typography key={idx} variant="body2">
                  {idx + 1}. {note}
                </Typography>
              ))}
              {waves.map((wave) => (
                <Accordion
                  key={wave.wave_id}
                  disableGutters
                  expanded={expandedWave === wave.wave_id}
                  onChange={(_, isExpanded) => setExpandedWave(isExpanded ? wave.wave_id : false)}
                >
                  <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Chip label={`Wave ${wave.wave_id}`} size="small" color="primary" />
                      <Typography fontWeight={600}>{wave.name}</Typography>
                      <Chip label={`${wave.tables.length} tables`} size="small" variant="outlined" />
                    </Stack>
                  </AccordionSummary>
                  <AccordionDetails>
                    {(() => {
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
                      return (
                        <Stack direction="row" spacing={1} sx={{ mb: 1, flexWrap: "wrap" }} useFlexGap>
                          <Chip size="small" label={`Tables: ${wave.tables.length}`} />
                          <Chip size="small" label={`Schemas: ${schemas.length}`} />
                          {waveDomains.length === 1 ? (
                            <Chip size="small" label={`Domain: ${waveDomains[0]}`} />
                          ) : (
                            <Chip size="small" label={`Domains: ${waveDomains.length}`} />
                          )}
                          <Chip size="small" color="success" label={`Green: ${queueCounts.green}`} />
                          <Chip size="small" color="warning" label={`Yellow: ${queueCounts.yellow}`} />
                          <Chip size="small" color="error" label={`Red: ${queueCounts.red}`} />
                          {queueCounts.unknown > 0 && <Chip size="small" label={`Unknown: ${queueCounts.unknown}`} />}
                        </Stack>
                      );
                    })()}
                    <Grid2 container spacing={2}>
                      <Grid2 size={{ xs: 12, md: 6 }}>
                        <Typography variant="subtitle2">Business rationale</Typography>
                        <Typography variant="body2" sx={{ mb: 1 }}>
                          {wave.business_rationale}
                        </Typography>
                      </Grid2>
                      <Grid2 size={{ xs: 12, md: 6 }}>
                        <Typography variant="subtitle2">Technical rationale</Typography>
                        <Typography variant="body2" sx={{ mb: 1 }}>
                          {wave.technical_rationale}
                        </Typography>
                      </Grid2>
                      <Grid2 size={{ xs: 12 }}>
                        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                          Tables in this wave
                        </Typography>
                        <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mb: 1 }} useFlexGap flexWrap="wrap">
                          <TextField
                            size="small"
                            label="Search table"
                            value={tableSearch}
                            onChange={(e) => setTableSearch(e.target.value)}
                            fullWidth
                          />
                          <TextField
                            select
                            size="small"
                            label="Queue"
                            value={queueFilter}
                            onChange={(e) => setQueueFilter(e.target.value)}
                            sx={{ minWidth: 130 }}
                          >
                            <MenuItem value="all">All</MenuItem>
                            <MenuItem value="green">Green</MenuItem>
                            <MenuItem value="yellow">Yellow</MenuItem>
                            <MenuItem value="red">Red</MenuItem>
                          </TextField>
                        </Stack>
                        {Array.from(new Set(wave.tables.map((t) => t.business_domain).filter((d) => Boolean(String(d).trim())))).length ===
                          1 && (
                          <Typography variant="body2" color="text.secondary" sx={{ mb: 0.8 }}>
                            Domain: {wave.tables[0]?.business_domain || "-"}
                          </Typography>
                        )}
                        <Table size="small">
                          <TableHead>
                            <TableRow>
                              <TableCell>#</TableCell>
                              <TableCell>Table</TableCell>
                              <TableCell>Schema</TableCell>
                              <TableCell>Queue</TableCell>
                              <TableCell align="right">Confidence</TableCell>
                              <TableCell align="right">Criticality</TableCell>
                              {Array.from(new Set(wave.tables.map((t) => t.business_domain).filter((d) => Boolean(String(d).trim()))))
                                .length > 1 && <TableCell>Domain</TableCell>}
                            </TableRow>
                          </TableHead>
                          <TableBody>
                            {wave.tables
                              .filter((t) => {
                                const scored = scoredByTable[t.full_name];
                                if (queueFilter !== "all" && scored?.queue !== queueFilter) return false;
                                if (!tableSearch.trim()) return true;
                                return t.full_name.toLowerCase().includes(tableSearch.trim().toLowerCase());
                              })
                              .map((t, idx) => {
                              const scored = scoredByTable[t.full_name];
                              const schema = String(t.full_name || "").split(".")[0] || "-";
                              return (
                              <TableRow key={t.full_name}>
                                <TableCell>{idx + 1}</TableCell>
                                <TableCell>{t.full_name}</TableCell>
                                <TableCell>{schema}</TableCell>
                                <TableCell>{scored?.queue ?? "-"}</TableCell>
                                <TableCell align="right">{scored?.confidence ?? "-"}</TableCell>
                                <TableCell align="right">{scored?.criticality ?? "-"}</TableCell>
                                {Array.from(new Set(wave.tables.map((x) => x.business_domain).filter((d) => Boolean(String(d).trim()))))
                                  .length > 1 && <TableCell>{t.business_domain || "-"}</TableCell>}
                              </TableRow>
                              );
                            })}
                          </TableBody>
                        </Table>
                      </Grid2>
                    </Grid2>
                  </AccordionDetails>
                </Accordion>
              ))}
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

