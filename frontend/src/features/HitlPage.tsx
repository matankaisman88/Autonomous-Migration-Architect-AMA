import { Button, Chip, Grid2, List, ListItem, ListItemText, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useRequireReportId, useErrorSetter } from "./common";

export function HitlPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [decisionFilter, setDecisionFilter] = useState("all");
  const [search, setSearch] = useState("");
  const decisionCount = Object.keys((data?.decisions as Record<string, unknown> | undefined) ?? {}).length;
  const decisions = (data?.decisions as Record<string, { action?: string }> | undefined) ?? {};
  const shownDecisions = useMemo(
    () =>
      Object.entries(decisions).filter(([sig, d]) => {
        const action = String(d?.action || "").toLowerCase();
        if (decisionFilter !== "all" && action !== decisionFilter) return false;
        if (!search.trim()) return true;
        return sig.toLowerCase().includes(search.trim().toLowerCase()) || action.includes(search.trim().toLowerCase());
      }),
    [decisions, decisionFilter, search]
  );
  return (
    <Grid2 container spacing={2}>
      <Grid2 size={12}>
        <PageCard title="Human-in-the-loop Review">
          <Stack direction="row" spacing={1}>
            <Button
              variant="outlined"
              disabled={!reportId}
              onClick={async () => {
                try {
                  setData(await api.getHitl(reportId));
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Load Decisions
            </Button>
            <Button
              variant="contained"
              disabled={!reportId}
              onClick={async () => {
                try {
                  setData(await api.applyHitl(reportId));
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Apply to Report
            </Button>
          </Stack>
          <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
            <Chip label={`Decisions ${decisionCount}`} />
            {"applied" in (data ?? {}) && (
              <Chip label={`Applied: ${String((data as { applied?: boolean }).applied)}`} color="success" variant="outlined" />
            )}
          </Stack>
          {"counts" in (data ?? {}) && (
            <Stack sx={{ mt: 2 }}>
              <Typography variant="body2">
                Merged: {String((data as { counts?: { merged_entities?: number } }).counts?.merged_entities ?? "-")}
              </Typography>
              <Typography variant="body2">
                Review: {String((data as { counts?: { review_candidates?: number } }).counts?.review_candidates ?? "-")}
              </Typography>
              <Typography variant="body2">
                Trash: {String((data as { counts?: { trash_candidates?: number } }).counts?.trash_candidates ?? "-")}
              </Typography>
            </Stack>
          )}
          {decisionCount > 0 && (
            <>
              <Typography variant="subtitle2" sx={{ mt: 2 }}>
                Recent Decisions
              </Typography>
              <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mb: 1 }} useFlexGap flexWrap="wrap">
                <TextField
                  size="small"
                  label="Search signature/action"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  fullWidth
                />
                <TextField
                  select
                  size="small"
                  label="Action"
                  value={decisionFilter}
                  onChange={(e) => setDecisionFilter(e.target.value)}
                  sx={{ minWidth: 140 }}
                >
                  <MenuItem value="all">All</MenuItem>
                  <MenuItem value="accept">accept</MenuItem>
                  <MenuItem value="review">review</MenuItem>
                  <MenuItem value="reject">reject</MenuItem>
                </TextField>
              </Stack>
              <List dense sx={{ maxHeight: 200, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                {shownDecisions
                  .slice(0, 30)
                  .map(([sig, d]) => (
                    <ListItem key={sig}>
                      <ListItemText primary={sig.slice(0, 16)} secondary={String(d?.action || "")} />
                    </ListItem>
                  ))}
              </List>
            </>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

