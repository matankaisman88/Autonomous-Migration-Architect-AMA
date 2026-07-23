import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  FormControlLabel,
  Grid2,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  Typography
} from "@mui/material";
import Checkbox from "@mui/material/Checkbox";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { MigrationReviewGate, PendingReviewBanner } from "./hitl-shared";
import { useRequireReportId, useErrorSetter } from "./common";

type CheckpointModel = {
  table_key?: string;
  model_name?: string;
  review_required?: boolean;
  is_stub?: boolean;
  sql?: string;
  generation_mode?: string;
};

type CheckpointA = {
  wave_summary?: string;
  generated_models?: CheckpointModel[];
  review_required_tables?: string[];
  fallback_active?: boolean;
};

export function CockpitPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [jobId, setJobId] = useState("");
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [approveResult, setApproveResult] = useState<Record<string, unknown> | null>(null);
  const [runExecution, setRunExecution] = useState(false);
  const [reviewAcknowledged, setReviewAcknowledged] = useState(false);
  const [isApproving, setIsApproving] = useState(false);
  const [targetDialect, setTargetDialect] = useState("duckdb");
  const [hitlAcknowledged, setHitlAcknowledged] = useState(false);
  const [pendingReview, setPendingReview] = useState(0);

  const job = (data?.job as Record<string, unknown> | undefined) ?? {};
  const checkpointA = (data?.checkpoint_a as CheckpointA | undefined) ?? undefined;
  const status = String(job.status ?? "").toUpperCase();
  const executionStatus = String(job.execution_status ?? "").toUpperCase();
  const total = Number(job.total_models ?? 0);
  const done = Number(job.completed_models ?? 0);
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const artifactAvailable = Boolean(checkpointA);
  const alreadyApproved = Boolean(job.checkpoint_a_approved);
  const reviewRequired = checkpointA?.review_required_tables ?? [];
  const models = checkpointA?.generated_models ?? [];
  const pollActive =
    Boolean(jobId) &&
    (status === "RUNNING" ||
      status === "QUEUED" ||
      executionStatus === "RUNNING" ||
      (!status && !executionStatus));

  const canApprove =
    status === "SUCCESS" &&
    artifactAvailable &&
    !alreadyApproved &&
    !isApproving &&
    (reviewRequired.length === 0 || reviewAcknowledged) &&
    (pendingReview === 0 || hitlAcknowledged);

  useEffect(() => {
    if (!jobId || !pollActive) return;
    const timer = window.setInterval(async () => {
      try {
        const polled = await api.pollCheckpointA(jobId);
        setData(polled);
        const genStatus = String(((polled.job as { status?: string } | undefined)?.status ?? "")).toUpperCase();
        const execStatus = String(
          ((polled.job as { execution_status?: string } | undefined)?.execution_status ?? "")
        ).toUpperCase();
        if (
          genStatus !== "RUNNING" &&
          genStatus !== "QUEUED" &&
          execStatus !== "RUNNING"
        ) {
          window.clearInterval(timer);
        }
      } catch (e) {
        setError(e);
        window.clearInterval(timer);
      }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [jobId, pollActive, setError]);

  const executionSummary = useMemo(() => {
    const modelState = (job.execution_model_state as Record<string, string> | undefined) ?? {};
    const entries = Object.entries(modelState);
    if (!entries.length) return null;
    return entries.map(([name, st]) => `${name}: ${st}`).join(", ");
  }, [job.execution_model_state]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12 }}>
        <PendingReviewBanner reportId={reportId} onPendingChange={setPendingReview} />
      </Grid2>
      <Grid2 size={{ xs: 12, md: 4 }}>
        <PageCard title="Checkpoint-A Job">
          <Stack spacing={1.5}>
            <MigrationReviewGate
              reportId={reportId}
              acknowledged={hitlAcknowledged}
              onAcknowledgedChange={setHitlAcknowledged}
            />
            <TextField
              select
              size="small"
              label="Target dialect"
              value={targetDialect}
              onChange={(e) => setTargetDialect(e.target.value)}
            >
              <MenuItem value="duckdb">duckdb</MenuItem>
              <MenuItem value="snowflake">snowflake</MenuItem>
              <MenuItem value="bigquery">bigquery</MenuItem>
              <MenuItem value="redshift">redshift</MenuItem>
            </TextField>
            <Button
              variant="contained"
              disabled={!reportId || status === "RUNNING" || (pendingReview > 0 && !hitlAcknowledged)}
              onClick={async () => {
                try {
                  if (pendingReview > 0 && !hitlAcknowledged) {
                    setError(`${pendingReview} column mapping(s) still need review.`);
                    return;
                  }
                  setApproveResult(null);
                  setReviewAcknowledged(false);
                  const started = await api.startCheckpointA(reportId, { target_dialect: targetDialect });
                  setData(started);
                  setJobId(String(started.job_id ?? ""));
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Generate Checkpoint-A
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
            {status && <Chip label={status} color={status === "FAILED" ? "error" : "primary"} size="small" />}
            {executionStatus && (
              <Chip
                label={`Execution: ${executionStatus}`}
                color={executionStatus === "FAILED" || executionStatus === "HITL_REQUIRED" ? "warning" : "success"}
                size="small"
                variant="outlined"
              />
            )}
            {alreadyApproved && (
              <Chip label="Checkpoint-A approved" color="success" size="small" />
            )}
          </Stack>
        </PageCard>

        {artifactAvailable && !alreadyApproved && (
          <Box sx={{ mt: 2 }}>
            <PageCard title="Approve Checkpoint-A">
              <Stack spacing={1.5}>
              {reviewRequired.length > 0 && (
                <Alert severity="warning">
                  {reviewRequired.length} table(s) flagged for review. Inspect proposed SQL below before approving.
                </Alert>
              )}
              {reviewRequired.length > 0 && (
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={reviewAcknowledged}
                      onChange={(e) => setReviewAcknowledged(e.target.checked)}
                    />
                  }
                  label="I have reviewed flagged tables"
                />
              )}
              <FormControlLabel
                control={
                  <Checkbox checked={runExecution} onChange={(e) => setRunExecution(e.target.checked)} />
                }
                label="Run dbt after writing files"
              />
              <Button
                variant="contained"
                color="success"
                disabled={!canApprove}
                onClick={async () => {
                  try {
                    setIsApproving(true);
                    const result = await api.approveCheckpointA(jobId, { run_execution: runExecution });
                    setApproveResult(result);
                    setData(await api.pollCheckpointA(jobId));
                  } catch (e) {
                    setError(e);
                  } finally {
                    setIsApproving(false);
                  }
                }}
              >
                {isApproving ? "Approving…" : runExecution ? "Approve & Run dbt" : "Approve & Write Files"}
              </Button>
              </Stack>
            </PageCard>
          </Box>
        )}
      </Grid2>

      <Grid2 size={{ xs: 12, md: 8 }}>
        <PageCard title="Cockpit Output">
          <Stack spacing={1.5}>
            <Typography variant="body2">Job ID: {jobId || "—"}</Typography>
            <Typography variant="body2">
              Models: {done} / {total || "—"}
            </Typography>
            <LinearProgress variant={total > 0 ? "determinate" : "indeterminate"} value={pct} />

            {alreadyApproved && (
              <Alert severity="success">
                Wrote {String(job.written_count ?? approveResult?.written_count ?? "?")} model file(s) to disk.
                {executionStatus === "RUNNING" && " dbt execution in progress…"}
              </Alert>
            )}

            {approveResult && !alreadyApproved && (
              <Alert severity="info">{JSON.stringify(approveResult)}</Alert>
            )}

            {executionStatus && executionStatus !== "RUNNING" && (
              <Alert
                severity={
                  executionStatus === "SUCCESS" ? "success" : executionStatus === "PARTIAL" ? "warning" : "error"
                }
              >
                Execution finished: {executionStatus}
                {job.execution_error ? ` — ${String(job.execution_error)}` : ""}
                {executionSummary ? (
                  <Typography variant="caption" component="div" sx={{ mt: 0.5 }}>
                    {executionSummary}
                  </Typography>
                ) : null}
              </Alert>
            )}

            {checkpointA?.wave_summary && (
              <Typography variant="body2" color="text.secondary">
                {checkpointA.wave_summary}
              </Typography>
            )}

            {checkpointA?.fallback_active && (
              <Alert severity="warning">Some models used non-AI fallback generation.</Alert>
            )}

            {artifactAvailable && models.length > 0 && (
              <>
                <Typography variant="subtitle2" sx={{ mt: 1 }}>
                  Proposed models ({models.length})
                </Typography>
                {models.map((model) => (
                  <Accordion key={model.model_name ?? model.table_key} disableGutters>
                    <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                      <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
                        <Typography variant="body2">{model.table_key ?? model.model_name}</Typography>
                        {model.review_required && (
                          <Chip label="review required" size="small" color="warning" />
                        )}
                        {model.is_stub && <Chip label="stub" size="small" color="error" variant="outlined" />}
                        {model.generation_mode && model.generation_mode !== "ai" && (
                          <Chip label={model.generation_mode} size="small" variant="outlined" />
                        )}
                      </Stack>
                    </AccordionSummary>
                    <AccordionDetails>
                      <Typography variant="caption" color="text.secondary">
                        Model: {model.model_name}
                      </Typography>
                      <Box
                        component="pre"
                        sx={{
                          mt: 1,
                          p: 1.5,
                          overflow: "auto",
                          maxHeight: 240,
                          bgcolor: "#0f172a",
                          color: "#e2e8f0",
                          fontSize: 12,
                          borderRadius: 1
                        }}
                      >
                        {model.sql ?? "-- no sql"}
                      </Box>
                    </AccordionDetails>
                  </Accordion>
                ))}
              </>
            )}

            {!artifactAvailable && status === "SUCCESS" && (
              <Typography variant="body2" color="text.secondary">
                Job succeeded but no Checkpoint-A artifact was returned.
              </Typography>
            )}
          </Stack>
        </PageCard>
      </Grid2>
    </Grid2>
  );
}
