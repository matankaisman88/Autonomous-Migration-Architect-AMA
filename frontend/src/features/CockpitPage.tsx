import { Button, Chip, Grid2, LinearProgress, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useRequireReportId, useErrorSetter } from "./common";

export function CockpitPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [jobId, setJobId] = useState("");
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [artifactFilter, setArtifactFilter] = useState("all");
  const status = String(((data?.job as { status?: string } | undefined)?.status ?? "")).toUpperCase();
  const total = Number((data?.job as { total_models?: number } | undefined)?.total_models ?? 0);
  const done = Number((data?.job as { completed_models?: number } | undefined)?.completed_models ?? 0);
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const showStatus = statusFilter === "all" || status.toLowerCase() === statusFilter;
  const artifactAvailable = Boolean(data?.checkpoint_a);
  const showArtifact = artifactFilter === "all" || (artifactFilter === "with" ? artifactAvailable : !artifactAvailable);
  const visible = useMemo(() => showStatus && showArtifact, [showStatus, showArtifact]);

  useEffect(() => {
    if (!jobId) return;
    const timer = window.setInterval(async () => {
      try {
        const polled = await api.pollCheckpointA(jobId);
        setData(polled);
        const currentStatus = String(((polled.job as { status?: string } | undefined)?.status ?? "")).toUpperCase();
        if (currentStatus === "SUCCESS" || currentStatus === "FAILED") {
          window.clearInterval(timer);
        }
      } catch (e) {
        setError(e);
        window.clearInterval(timer);
      }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [jobId, setError]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12, md: 4 }}>
        <PageCard title="Checkpoint-A Job">
          <Stack spacing={1}>
            <Button
              variant="contained"
              disabled={!reportId}
              onClick={async () => {
                try {
                  const started = await api.startCheckpointA(reportId);
                  setData(started);
                  setJobId(String(started.job_id ?? ""));
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Start Job
            </Button>
            <Button
              variant="outlined"
              disabled={!jobId}
              onClick={async () => {
                try {
                  setData(await api.pollCheckpointA(jobId));
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Poll Now
            </Button>
            <TextField
              select
              size="small"
              label="Status filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              sx={{ minWidth: 170 }}
            >
              <MenuItem value="all">All</MenuItem>
              <MenuItem value="success">SUCCESS</MenuItem>
              <MenuItem value="failed">FAILED</MenuItem>
              <MenuItem value="running">RUNNING</MenuItem>
              <MenuItem value="queued">QUEUED</MenuItem>
            </TextField>
            <TextField
              select
              size="small"
              label="Artifact filter"
              value={artifactFilter}
              onChange={(e) => setArtifactFilter(e.target.value)}
              sx={{ minWidth: 170 }}
            >
              <MenuItem value="all">All</MenuItem>
              <MenuItem value="with">With artifact</MenuItem>
              <MenuItem value="without">Without artifact</MenuItem>
            </TextField>
            {status && <Chip label={status} color={status === "FAILED" ? "error" : "primary"} />}
          </Stack>
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12, md: 8 }}>
        <PageCard title="Cockpit Output">
          {!visible ? (
            <Typography variant="body2" color="text.secondary">
              Current result is hidden by filters.
            </Typography>
          ) : (
          <Stack spacing={1}>
            <Typography variant="body2">Job ID: {jobId || "-"}</Typography>
            <Typography variant="body2">Completed models: {done}</Typography>
            <Typography variant="body2">Total models: {total}</Typography>
            <LinearProgress variant={total > 0 ? "determinate" : "indeterminate"} value={pct} />
            {data?.checkpoint_a && (
              <Typography variant="body2" color="text.secondary">
                Checkpoint-A artifact available.
              </Typography>
            )}
          </Stack>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

