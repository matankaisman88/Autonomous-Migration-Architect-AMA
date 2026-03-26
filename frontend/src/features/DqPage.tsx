import { Button, Chip, Grid2, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField } from "@mui/material";
import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useRequireReportId, useErrorSetter } from "./common";

type Check = { name: string; severity: "ok" | "warn" | "error"; message: string };

export function DqPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [data, setData] = useState<{ ok?: boolean; checks?: Check[] } | null>(null);
  const [severityFilter, setSeverityFilter] = useState<"all" | "ok" | "warn" | "error">("all");
  const [search, setSearch] = useState("");
  const ok = useMemo(() => Boolean(data && (data as { ok?: boolean }).ok), [data]);
  const shownChecks = useMemo(
    () =>
      (data?.checks ?? []).filter((c) => {
        if (severityFilter !== "all" && c.severity !== severityFilter) return false;
        if (!search.trim()) return true;
        const q = search.trim().toLowerCase();
        return c.name.toLowerCase().includes(q) || c.message.toLowerCase().includes(q);
      }),
    [data, severityFilter, search]
  );
  return (
    <Grid2 container spacing={2}>
      <Grid2 size={12}>
        <PageCard
          title="Data Quality"
          action={ok ? <Chip label="Healthy" color="success" /> : <Chip label="Unknown" variant="outlined" />}
        >
          <Stack direction="row" spacing={1}>
            <Button
              variant="contained"
              disabled={!reportId}
              onClick={async () => {
                try {
                  setData((await api.runDq(reportId)) as { ok?: boolean; checks?: Check[] });
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Run DQ Suite
            </Button>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1} sx={{ mt: 1 }} useFlexGap flexWrap="wrap">
            <TextField
              size="small"
              label="Search checks"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              fullWidth
            />
            <TextField
              select
              size="small"
              label="Severity"
              value={severityFilter}
              onChange={(e) => setSeverityFilter(e.target.value as "all" | "ok" | "warn" | "error")}
              sx={{ minWidth: 140 }}
            >
              <MenuItem value="all">All</MenuItem>
              <MenuItem value="ok">OK</MenuItem>
              <MenuItem value="warn">Warn</MenuItem>
              <MenuItem value="error">Error</MenuItem>
            </TextField>
          </Stack>
          <Table size="small" sx={{ mt: 1 }}>
            <TableHead>
              <TableRow>
                <TableCell>Check</TableCell>
                <TableCell>Severity</TableCell>
                <TableCell>Message</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {shownChecks.map((c) => (
                <TableRow key={`${c.name}-${c.message}`}>
                  <TableCell>{c.name}</TableCell>
                  <TableCell>
                    <Chip
                      label={c.severity.toUpperCase()}
                      size="small"
                      color={c.severity === "error" ? "error" : c.severity === "warn" ? "warning" : "success"}
                    />
                  </TableCell>
                  <TableCell>{c.message}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

