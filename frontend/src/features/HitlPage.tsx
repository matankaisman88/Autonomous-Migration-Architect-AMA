import { Alert, Chip, Grid2, LinearProgress, Stack, Typography } from "@mui/material";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { MappingReviewList } from "./hitl-shared";
import { useRequireReportId, useErrorSetter } from "./common";
import type { HitlQueueResponse } from "../hitl-types";

export function HitlPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [queue, setQueue] = useState<HitlQueueResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!reportId) return;
    setLoading(true);
    try {
      setQueue(await api.hitlQueue(reportId));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [reportId, setError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={12}>
        <PageCard title="Column Mapping Review">
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            AMA found ambiguous Hebrew/legacy column names in SQL logs. Approve or reject each suggested mapping before
            bulk dbt generation. Decisions update the migration plan automatically — no sidecar file management needed.
          </Typography>
          {loading && !queue ? <LinearProgress sx={{ mb: 2 }} /> : null}
          {queue && (
            <Stack direction="row" spacing={1} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
              <Chip label={`Merged ${queue.counts.merged_entities}`} color="success" variant="outlined" />
              <Chip label={`Pending ${queue.pending_count}`} color={queue.pending_count > 0 ? "warning" : "default"} />
              <Chip label={`Rejected ${queue.counts.trash_candidates}`} variant="outlined" />
            </Stack>
          )}
          {queue &&
          queue.pending_count === 0 &&
          (queue.rejected_count ?? 0) === 0 &&
          queue.items.length === 0 ? (
            <Alert severity="success" variant="outlined">
              No ambiguous mappings in this report. You can proceed to Tables, Bulk, or Cockpit.
            </Alert>
          ) : (
            <>
              {queue && (queue.rejected_count ?? 0) > 0 && queue.pending_count === 0 ? (
                <Alert severity="warning" variant="outlined" sx={{ mb: 2 }}>
                  {queue.rejected_count} mapping{queue.rejected_count === 1 ? "" : "s"} rejected — those columns are
                  unmapped for migration. Re-run <strong>Evaluate</strong> on Tables; affected tables move to{" "}
                  <strong>yellow (Review)</strong>.
                </Alert>
              ) : null}
              <MappingReviewList reportId={reportId} onQueueChange={setQueue} maxHeight={560} />
            </>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}
