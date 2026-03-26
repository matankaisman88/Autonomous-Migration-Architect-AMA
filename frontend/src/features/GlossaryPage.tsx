import { useMemo, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useErrorSetter, useRequireReportId } from "./common";
import {
  Box,
  Button,
  Chip,
  Grid2,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography
} from "@mui/material";

type GlossaryEntry = {
  business_term?: string;
  definition?: string;
  target_ddl?: string;
  legacy_columns?: string;
  source_tables?: string[];
  domain?: string;
  kind?: string;
  confidence?: number;
  confidence_display?: number;
};

export function GlossaryPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [confMin, setConfMin] = useState(0);
  const [portfolio, setPortfolio] = useState("All");
  const [domains, setDomains] = useState("");
  const [status, setStatus] = useState("All");
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState("Business term (A-Z)");
  const [entries, setEntries] = useState<GlossaryEntry[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});

  const domainOptions = useMemo(() => {
    const all = Array.from(new Set(entries.map((e) => e.domain).filter(Boolean)));
    return ["", ...all];
  }, [entries]);

  const shown = useMemo(() => {
    let out = [...entries];
    if (status === "Confirmed") out = out.filter((e) => String(e.kind || "").toLowerCase() === "confirmed");
    else if (status === "Needs review") out = out.filter((e) => String(e.kind || "").toLowerCase() === "review");
    else if (status === "Glossary files") out = out.filter((e) => String(e.kind || "").toLowerCase() === "glossary_source");
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      out = out.filter(
        (e) =>
          String(e.business_term || "").toLowerCase().includes(q) ||
          String(e.definition || "").toLowerCase().includes(q) ||
          String(e.legacy_columns || "").toLowerCase().includes(q) ||
          String(e.domain || "").toLowerCase().includes(q) ||
          (e.source_tables || []).join(" ").toLowerCase().includes(q)
      );
    }
    if (sortBy === "Confidence (high first)") {
      out.sort(
        (a, b) =>
          Number(b.confidence_display ?? b.confidence ?? 0) - Number(a.confidence_display ?? a.confidence ?? 0)
      );
    } else {
      out.sort((a, b) => String(a.business_term || "").localeCompare(String(b.business_term || "")));
    }
    return out;
  }, [entries, search, sortBy, status]);

  async function loadGlossary() {
    try {
      const res = await api.glossary(reportId, confMin, portfolio, domains);
      setEntries((res.entries ?? []) as GlossaryEntry[]);
      setCounts(res.counts ?? {});
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12 }}>
        <PageCard title="Semantic Glossary">
          <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
            <TextField
              label="Min confidence"
              type="number"
              size="small"
              value={confMin}
              onChange={(e) => setConfMin(Number(e.target.value || 0))}
            />
            <TextField
              select
              label="Portfolio"
              size="small"
              value={portfolio}
              onChange={(e) => setPortfolio(e.target.value)}
              sx={{ minWidth: 140 }}
            >
              <MenuItem value="All">All</MenuItem>
              <MenuItem value="Core">Core</MenuItem>
              <MenuItem value="LongTail">LongTail</MenuItem>
            </TextField>
            <TextField select label="Domain" size="small" value={domains} onChange={(e) => setDomains(e.target.value)} sx={{ minWidth: 180 }}>
              <MenuItem value="">All</MenuItem>
              {domainOptions
                .filter((d) => d)
                .map((d) => (
                  <MenuItem key={d} value={d}>
                    {d}
                  </MenuItem>
                ))}
            </TextField>
            <Button variant="contained" onClick={loadGlossary} disabled={!reportId}>
              Load Glossary
            </Button>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
            <TextField
              label="Search glossary"
              size="small"
              fullWidth
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="term, definition, logs name, table..."
            />
            <TextField select label="Status" size="small" value={status} onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 150 }}>
              <MenuItem value="All">All</MenuItem>
              <MenuItem value="Confirmed">Confirmed</MenuItem>
              <MenuItem value="Needs review">Needs review</MenuItem>
              <MenuItem value="Glossary files">Glossary files</MenuItem>
            </TextField>
            <TextField select label="Sort by" size="small" value={sortBy} onChange={(e) => setSortBy(e.target.value)} sx={{ minWidth: 190 }}>
              <MenuItem value="Business term (A-Z)">Business term (A-Z)</MenuItem>
              <MenuItem value="Confidence (high first)">Confidence (high first)</MenuItem>
            </TextField>
          </Stack>
          <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap" }}>
            <Chip label={`Entries ${shown.length}`} />
            {Object.entries(counts).map(([k, v]) => (
              <Chip key={k} label={`${k}: ${v}`} color="primary" variant="outlined" />
            ))}
          </Stack>

          <Box sx={{ maxHeight: 520, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Business label</TableCell>
                  <TableCell>Domain</TableCell>
                  <TableCell>Canonical column (target)</TableCell>
                  <TableCell>Names in SQL / logs</TableCell>
                  <TableCell>Tables</TableCell>
                  <TableCell>Confidence</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Definition</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {shown.map((e, idx) => (
                  <TableRow key={`${e.business_term ?? "term"}-${idx}`} hover>
                    <TableCell>{e.business_term ?? "-"}</TableCell>
                    <TableCell>{e.domain ?? "-"}</TableCell>
                    <TableCell>{e.target_ddl ?? "-"}</TableCell>
                    <TableCell>{e.legacy_columns ?? "-"}</TableCell>
                    <TableCell>{(e.source_tables || []).join(", ") || "-"}</TableCell>
                    <TableCell>{e.confidence_display ?? e.confidence ?? "-"}</TableCell>
                    <TableCell>{e.kind ?? "-"}</TableCell>
                    <TableCell sx={{ maxWidth: 560 }}>
                      <Typography variant="body2">{e.definition ?? "-"}</Typography>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        </PageCard>
      </Grid2>
    </Grid2>
  );
}
