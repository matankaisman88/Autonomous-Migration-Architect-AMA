import { Box, Button, Checkbox, FormControlLabel, Grid2, LinearProgress, List, ListItem, ListItemText, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useEffect, useMemo, useState } from "react";
import { api, bulkWsUrl } from "../api";
import { PageCard } from "../components/PageCard";
import { QueueChip, useRequireReportId, useErrorSetter } from "./common";
import type { BulkJob } from "../types";
import type { ScoredTable } from "../types";
import { useAppState } from "../state";

export function BulkPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const { setNotice } = useAppState();
  const [job, setJob] = useState<BulkJob | null>(null);
  const [evalRows, setEvalRows] = useState<ScoredTable[]>([]);
  const [greenCandidates, setGreenCandidates] = useState<string[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [skipped, setSkipped] = useState<{ table_key: string; reason: string }[]>([]);
  const [dialect, setDialect] = useState("duckdb");
  const [dryRun, setDryRun] = useState(false);
  const [confFloor, setConfFloor] = useState(50);
  const [critCeil, setCritCeil] = useState(80);
  const [autoSelectAllGreen, setAutoSelectAllGreen] = useState(false);
  const [jobId, setJobId] = useState("");
  const [contractPreview, setContractPreview] = useState<{ contract_id: string; rules: string[]; excluded: string[] } | null>(null);
  const [candidateSearch, setCandidateSearch] = useState("");
  const [resultSearch, setResultSearch] = useState("");
  const progress = useMemo(() => {
    if (!job || !job.total) return 0;
    return Math.round((job.completed / job.total) * 100);
  }, [job]);
  const yellowCount = useMemo(() => evalRows.filter((r) => r.queue === "yellow").length, [evalRows]);
  const redCount = useMemo(() => evalRows.filter((r) => r.queue === "red").length, [evalRows]);
  const visibleCandidates = useMemo(
    () =>
      greenCandidates.filter((k) =>
        candidateSearch.trim() ? k.toLowerCase().includes(candidateSearch.trim().toLowerCase()) : true
      ),
    [greenCandidates, candidateSearch]
  );
  const shownSuccess = useMemo(
    () =>
      (job?.success ?? []).filter((t) =>
        resultSearch.trim() ? t.toLowerCase().includes(resultSearch.trim().toLowerCase()) : true
      ),
    [job, resultSearch]
  );
  const shownFailed = useMemo(
    () =>
      (job?.failed ?? []).filter((f) =>
        resultSearch.trim() ? f.table_key.toLowerCase().includes(resultSearch.trim().toLowerCase()) : true
      ),
    [job, resultSearch]
  );

  async function prepareBulk() {
    try {
      const ev = await api.evaluate(reportId);
      setEvalRows(ev.scored_tables);
      setContractPreview({
        contract_id: String(ev.contract_preview?.contract_id ?? ""),
        rules: Array.isArray(ev.contract_preview?.rules) ? ev.contract_preview.rules : [],
        excluded: Array.isArray(ev.contract_preview?.excluded) ? ev.contract_preview.excluded : []
      });
      const keys = ev.scored_tables
        .filter((s) => s.queue === "green")
        .filter((s) => s.confidence >= confFloor)
        .filter((s) => s.criticality <= critCeil)
        .map((s) => s.table_key);
      setGreenCandidates(keys);
      setSelectedKeys(autoSelectAllGreen ? keys : keys.slice(0, Math.min(keys.length, 10)));
    } catch (e) {
      setError(e);
    }
  }

  async function startBulk() {
    try {
      if (dryRun) {
        setNotice("Dry Run is UI-only currently. Disable Dry Run to execute migration.");
        return;
      }
      const started = await api.startBulk(reportId, selectedKeys, dialect);
      setJobId(started.job_id);
      setSkipped(started.skipped ?? []);
      const ws = new WebSocket(bulkWsUrl(started.job_id));
      ws.onmessage = (event) => {
        const payload = JSON.parse(event.data) as BulkJob;
        if ("status" in payload) setJob(payload);
      };
      ws.onerror = () => setError("Bulk websocket failed.");
    } catch (e) {
      setError(e);
    }
  }

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const latest = await api.getBulkStatus(jobId);
        if (cancelled) return;
        setJob(latest);
        if (latest.status === "done" || latest.status === "failed") {
          clearInterval(timer);
        }
      } catch {
        // websocket may still be active; keep trying quietly
      }
    }, 1200);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [jobId]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12, md: 5 }}>
        <PageCard title="Bulk Migration Controls">
          <Stack spacing={1}>
            <Stack direction={{ xs: "column", md: "row" }} spacing={1} useFlexGap flexWrap="wrap">
              <TextField
                label="Confidence Floor"
                type="number"
                size="small"
                value={confFloor}
                onChange={(e) => setConfFloor(Number(e.target.value || 50))}
              />
              <TextField
                label="Criticality Ceiling"
                type="number"
                size="small"
                value={critCeil}
                onChange={(e) => setCritCeil(Number(e.target.value || 80))}
              />
              <TextField
                select
                label="Target Dialect"
                size="small"
                value={dialect}
                onChange={(e) => setDialect(e.target.value)}
                sx={{ minWidth: 170 }}
              >
                <MenuItem value="duckdb">duckdb</MenuItem>
                <MenuItem value="snowflake">snowflake</MenuItem>
                <MenuItem value="bigquery">bigquery</MenuItem>
                <MenuItem value="redshift">redshift</MenuItem>
              </TextField>
            </Stack>
            <FormControlLabel
              control={<Checkbox checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />}
              label="Dry Run"
            />
            <FormControlLabel
              control={<Checkbox checked={autoSelectAllGreen} onChange={(e) => setAutoSelectAllGreen(e.target.checked)} />}
              label="Auto-select all GREEN"
            />
            <Button variant="outlined" disabled={!reportId} onClick={prepareBulk}>
              Prepare GREEN Tables
            </Button>
            <Button variant="contained" disabled={!reportId || selectedKeys.length === 0} onClick={startBulk}>
              Start Bulk Run
            </Button>
            {jobId && (
              <Button
                variant="text"
                color="inherit"
                onClick={async () => {
                  try {
                    await api.clearBulkJob(jobId);
                    setJob(null);
                    setJobId("");
                    setNotice("Bulk job status cleared.");
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                Clear Bulk Status
              </Button>
            )}
            <Typography variant="body2" color="text.secondary">
              Candidates: {greenCandidates.length} | Selected: {selectedKeys.length}
            </Typography>
            <Stack direction="row" spacing={1.5} alignItems="center" useFlexGap flexWrap="wrap">
              <QueueChip queue="green" />
              <Typography variant="caption">{greenCandidates.length}</Typography>
              <QueueChip queue="yellow" />
              <Typography variant="caption">{yellowCount}</Typography>
              <QueueChip queue="red" />
              <Typography variant="caption">{redCount}</Typography>
            </Stack>
            {contractPreview && (
              <Stack spacing={0.5} sx={{ p: 1, border: "1px solid #e2e8f0", borderRadius: 1 }}>
                <Typography variant="subtitle2">Contract Preview</Typography>
                <Typography variant="caption">contract_id: {contractPreview.contract_id || "-"}</Typography>
                <Typography variant="caption">rules: {contractPreview.rules.length}</Typography>
                <Typography variant="caption">excluded: {contractPreview.excluded.length}</Typography>
              </Stack>
            )}
            <List dense sx={{ maxHeight: 220, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
              <TextField
                size="small"
                label="Filter candidates"
                value={candidateSearch}
                onChange={(e) => setCandidateSearch(e.target.value)}
                sx={{ m: 1 }}
              />
              {visibleCandidates.map((key) => (
                <ListItem key={key} disableGutters>
                  <FormControlLabel
                    control={
                      <Checkbox
                        checked={selectedKeys.includes(key)}
                        onChange={(e) =>
                          setSelectedKeys((prev) =>
                            e.target.checked ? [...prev, key] : prev.filter((k) => k !== key)
                          )
                        }
                      />
                    }
                    label={key}
                  />
                </ListItem>
              ))}
            </List>
            {job && (
              <Box
                sx={{
                  p: 2,
                  border: "1px solid",
                  borderColor:
                    job.status === "done" ? "success.main" : job.status === "failed" ? "error.main" : "primary.main",
                  borderRadius: 2,
                  background: "rgba(56,189,248,0.04)"
                }}
              >
                <Stack direction="row" justifyContent="space-between" mb={1}>
                  <Typography variant="subtitle2">
                    {job.status === "done"
                      ? "✅ COMPLETE"
                      : job.status === "failed"
                        ? "❌ FAILED"
                        : `⚙️ MIGRATING - ${job.current_table || "..."}`}
                  </Typography>
                  <Typography variant="caption">
                    {job.completed} / {job.total} tables
                  </Typography>
                </Stack>
                <LinearProgress variant="determinate" value={progress} />
                <Stack direction="row" spacing={2} mt={1}>
                  <Typography variant="caption" color="success.main">
                    ✓ {job.success?.length ?? 0} succeeded
                  </Typography>
                  <Typography variant="caption" color="error.main">
                    ✗ {job.failed?.length ?? 0} failed
                  </Typography>
                </Stack>
              </Box>
            )}
          </Stack>
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12, md: 7 }}>
        <PageCard title="Job Details">
          {!job ? (
            <Typography variant="body2" color="text.secondary">
              Start a bulk run to see live details.
            </Typography>
          ) : (
            <Stack spacing={1.2}>
              <TextField
                size="small"
                label="Filter results"
                value={resultSearch}
                onChange={(e) => setResultSearch(e.target.value)}
              />
              <Typography variant="body2">Current table: {job.current_table || "-"}</Typography>
              <Typography variant="subtitle2">Succeeded ({shownSuccess.length})</Typography>
              <List dense sx={{ maxHeight: 180, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                {shownSuccess.map((t) => (
                  <ListItem key={t}>
                    <ListItemText primary={t} />
                  </ListItem>
                ))}
              </List>
              <Typography variant="subtitle2">Failed ({shownFailed.length})</Typography>
              <List dense sx={{ maxHeight: 180, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                {shownFailed.map((f) => (
                  <ListItem key={`${f.table_key}-${f.reason}`}>
                    <ListItemText primary={f.table_key} secondary={f.reason} />
                  </ListItem>
                ))}
              </List>
              <Typography variant="subtitle2">Skipped ({skipped.length})</Typography>
              <List dense sx={{ maxHeight: 160, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                {skipped.map((f) => (
                  <ListItem key={`${f.table_key}-${f.reason}`}>
                    <ListItemText primary={f.table_key} secondary={f.reason} />
                  </ListItem>
                ))}
              </List>
            </Stack>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

