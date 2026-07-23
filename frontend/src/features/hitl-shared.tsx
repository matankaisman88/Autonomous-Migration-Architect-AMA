import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  FormControlLabel,
  LinearProgress,
  Stack,
  Typography
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { HitlQueueItem, HitlQueueResponse } from "../hitl-types";
import { useErrorSetter } from "./common";

type PendingReviewBannerProps = {
  reportId: string;
  compact?: boolean;
  onPendingChange?: (count: number) => void;
};

export function PendingReviewBanner({ reportId, compact, onPendingChange }: PendingReviewBannerProps) {
  const [pending, setPending] = useState<number | null>(null);
  const [rejected, setRejected] = useState(0);

  const refresh = useCallback(async () => {
    if (!reportId) return;
    try {
      const q = await api.hitlQueue(reportId);
      setPending(q.pending_count);
      setRejected(q.rejected_count ?? 0);
      onPendingChange?.(q.pending_count);
    } catch {
      setPending(null);
      setRejected(0);
    }
  }, [reportId, onPendingChange]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (pending === null || (pending <= 0 && rejected <= 0)) return null;

  if (compact) {
    return (
      <Typography variant="caption" color="warning.main">
        {pending > 0 ? `${pending} mapping${pending === 1 ? "" : "s"} pending review` : null}
        {pending > 0 && rejected > 0 ? " · " : null}
        {rejected > 0 ? `${rejected} rejected — needs manual fix` : null}
        {" — "}
        <RouterLink to="/hitl">Review</RouterLink>
      </Typography>
    );
  }

  return (
    <Alert severity={rejected > 0 && pending <= 0 ? "error" : "warning"} variant="outlined" sx={{ borderRadius: 2 }}>
      <Typography variant="body2">
        {pending > 0 ? (
          <>
            <strong>
              {pending} column mapping{pending === 1 ? "" : "s"}
            </strong>{" "}
            need human review before bulk migration.{" "}
          </>
        ) : null}
        {rejected > 0 ? (
          <>
            <strong>
              {rejected} mapping{rejected === 1 ? "" : "s"}
            </strong>{" "}
            were rejected — affected tables should be reviewed manually (re-run <strong>Evaluate</strong> on Tables to
            refresh queue).{" "}
          </>
        ) : null}
        <RouterLink to="/hitl">Open mapping review →</RouterLink>
      </Typography>
    </Alert>
  );
};

type MigrationReviewGateProps = {
  reportId: string;
  acknowledged: boolean;
  onAcknowledgedChange: (value: boolean) => void;
};

export function MigrationReviewGate({ reportId, acknowledged, onAcknowledgedChange }: MigrationReviewGateProps) {
  const [pending, setPending] = useState(0);

  useEffect(() => {
    if (!reportId) return;
    void api.hitlQueue(reportId).then((q) => setPending(q.pending_count)).catch(() => setPending(0));
  }, [reportId]);

  if (pending <= 0) return null;

  return (
    <Alert severity="warning" sx={{ borderRadius: 1.5 }}>
      <Typography variant="body2" sx={{ mb: 1 }}>
        {pending} unresolved column mapping{pending === 1 ? "" : "s"} — review in{" "}
        <RouterLink to="/hitl">Column mapping review</RouterLink> first, or continue anyway.
      </Typography>
      <FormControlLabel
        control={<Checkbox checked={acknowledged} onChange={(e) => onAcknowledgedChange(e.target.checked)} />}
        label="I understand — continue without resolving pending mappings"
      />
    </Alert>
  );
}

type MappingReviewActionsProps = {
  reportId: string;
  row: HitlQueueItem["row"];
  signature?: string;
  compact?: boolean;
  disabled?: boolean;
  onDecided?: () => void;
};

export function MappingReviewActions({
  reportId,
  row,
  compact,
  disabled,
  onDecided
}: MappingReviewActionsProps) {
  const [busy, setBusy] = useState(false);
  const setError = useErrorSetter();

  async function decide(action: "approved" | "rejected" | "clear") {
    setBusy(true);
    try {
      await api.hitlDecide(reportId, row, action);
      onDecided?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const size = compact ? "small" : "medium";
  return (
    <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
      <Button size={size} variant="contained" color="success" disabled={disabled || busy} onClick={() => decide("approved")}>
        Approve
      </Button>
      <Button size={size} variant="outlined" color="error" disabled={disabled || busy} onClick={() => decide("rejected")}>
        Reject
      </Button>
    </Stack>
  );
}

type MappingReviewListProps = {
  reportId: string;
  sourceTable?: string;
  maxHeight?: number;
  onQueueChange?: (queue: HitlQueueResponse) => void;
};

export function MappingReviewList({ reportId, sourceTable, maxHeight = 420, onQueueChange }: MappingReviewListProps) {
  const [queue, setQueue] = useState<HitlQueueResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [statusFilter, setStatusFilter] = useState<"pending" | "all">("pending");
  const setError = useErrorSetter();

  const refresh = useCallback(async () => {
    if (!reportId) return;
    setLoading(true);
    try {
      const q = await api.hitlQueue(reportId, sourceTable);
      setQueue(q);
      onQueueChange?.(q);
      setSelected(new Set());
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [reportId, sourceTable, onQueueChange]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const shown = useMemo(() => {
    const items = queue?.items ?? [];
    if (statusFilter === "pending") return items.filter((i) => i.status === "pending");
    return items;
  }, [queue, statusFilter]);

  async function batchDecide(action: "approved" | "rejected", opts?: { min_confidence?: number; max_confidence?: number }) {
    if (!reportId) return;
    setLoading(true);
    try {
      await api.hitlBatchDecide(reportId, {
        action,
        signatures: selected.size > 0 ? Array.from(selected) : undefined,
        source_table: sourceTable,
        ...opts
      });
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  if (!queue) {
    return loading ? <LinearProgress /> : null;
  }

  if (queue.items.length === 0 && (queue.rejected_items?.length ?? 0) === 0) {
    return (
      <Alert severity="info" variant="outlined">
        No ambiguous column mappings in scope. AMA classified everything as confirmed or rejected automatically.
      </Alert>
    );
  }

  const rejectedShown = queue.rejected_items ?? [];

  return (
    <Stack spacing={1.5}>
      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" alignItems="center">
        <Chip label={`Pending ${queue.pending_count}`} color={queue.pending_count > 0 ? "warning" : "default"} />
        <Chip label={`Approved ${queue.approved_count}`} color="success" variant="outlined" />
        <Chip label={`Rejected ${queue.rejected_count}`} variant="outlined" />
        <Button size="small" variant={statusFilter === "pending" ? "contained" : "outlined"} onClick={() => setStatusFilter("pending")}>
          Pending only
        </Button>
        <Button size="small" variant={statusFilter === "all" ? "contained" : "outlined"} onClick={() => setStatusFilter("all")}>
          All
        </Button>
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          Refresh
        </Button>
      </Stack>

      {!sourceTable && queue.pending_count > 0 && (
        <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
          <Button size="small" variant="outlined" disabled={loading} onClick={() => batchDecide("approved", { min_confidence: 0.7 })}>
            Approve all ≥ 70%
          </Button>
          <Button size="small" variant="outlined" color="error" disabled={loading} onClick={() => batchDecide("rejected", { max_confidence: 0.4 })}>
            Reject all &lt; 40%
          </Button>
          {selected.size > 0 && (
            <>
              <Button size="small" variant="contained" color="success" disabled={loading} onClick={() => batchDecide("approved")}>
                Approve selected ({selected.size})
              </Button>
              <Button size="small" variant="outlined" color="error" disabled={loading} onClick={() => batchDecide("rejected")}>
                Reject selected ({selected.size})
              </Button>
            </>
          )}
        </Stack>
      )}

      <Box sx={{ maxHeight, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
        {shown.map((item) => {
          const row = item.row;
          const pending = item.status === "pending";
          return (
            <Box
              key={item.signature}
              sx={{
                p: 1.5,
                borderBottom: "1px solid #e2e8f0",
                bgcolor: pending ? "rgba(251,191,36,0.06)" : "transparent"
              }}
            >
              <Stack direction={{ xs: "column", md: "row" }} spacing={1} justifyContent="space-between" alignItems={{ md: "center" }}>
                <Stack direction="row" spacing={1} alignItems="flex-start">
                  {!sourceTable && pending && (
                    <Checkbox
                      size="small"
                      checked={selected.has(item.signature)}
                      onChange={(e) => {
                        setSelected((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(item.signature);
                          else next.delete(item.signature);
                          return next;
                        });
                      }}
                    />
                  )}
                  <Box>
                    <Typography variant="subtitle2">
                      <code>{row.legacy_name}</code> → <code>{row.suggested_ddl}</code>
                    </Typography>
                    <Typography variant="caption" color="text.secondary" display="block">
                      {row.source_table} · confidence {(item.merge_confidence * 100).toFixed(0)}%
                      {row.strategy ? ` · ${row.strategy}` : ""}
                    </Typography>
                    {row.citation ? (
                      <Typography variant="caption" color="text.secondary" display="block">
                        {row.citation}
                      </Typography>
                    ) : null}
                    {!pending && (
                      <Chip
                        size="small"
                        label={item.status}
                        color={item.status === "approved" ? "success" : "default"}
                        sx={{ mt: 0.5 }}
                      />
                    )}
                  </Box>
                </Stack>
                {pending && (
                  <MappingReviewActions reportId={reportId} row={row} compact onDecided={() => void refresh()} />
                )}
              </Stack>
            </Box>
          );
        })}
        {shown.length === 0 && rejectedShown.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
            No {statusFilter === "pending" ? "pending" : ""} items in this view.
          </Typography>
        )}
      </Box>

      {rejectedShown.length > 0 && (
        <>
          <Typography variant="subtitle2" sx={{ mt: 1 }}>
            Rejected mappings (manual fix required)
          </Typography>
          <Box sx={{ maxHeight: Math.min(maxHeight, 220), overflow: "auto", border: "1px solid #fecaca", borderRadius: 1 }}>
            {rejectedShown.map((item) => {
              const row = item.row;
              return (
                <Box key={item.signature} sx={{ p: 1.5, borderBottom: "1px solid #fee2e2", bgcolor: "rgba(254,226,226,0.25)" }}>
                  <Typography variant="subtitle2">
                    <code>{row.legacy_name}</code> → <code>{row.suggested_ddl}</code> (rejected)
                  </Typography>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {row.source_table} · AMA will not use this mapping — define the correct column in glossary or model SQL
                  </Typography>
                </Box>
              );
            })}
          </Box>
        </>
      )}
    </Stack>
  );
}
