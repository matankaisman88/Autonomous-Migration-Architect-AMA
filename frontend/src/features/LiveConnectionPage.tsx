import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormControlLabel,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  Typography
} from "@mui/material";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, liveWsUrl, type LiveStartPayload } from "../api";
import { PageCard } from "../components/PageCard";
import type { LiveIngestionSnapshot } from "../types";
import { useAppState } from "../state";

const MODES = ["sqlserver", "oracle", "db2"] as const;

export function LiveConnectionPage() {
  const navigate = useNavigate();
  const { setError, setReportId, setReportPath, setSummary, setNotice } = useAppState();
  const [mode, setMode] = useState<(typeof MODES)[number]>("sqlserver");
  const [connectionName, setConnectionName] = useState("");
  const [host, setHost] = useState("127.0.0.1");
  const [port, setPort] = useState("1433");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [database, setDatabase] = useState("");
  const [serviceName, setServiceName] = useState("XEPDB1");
  const [connString, setConnString] = useState("");
  const [schemasText, setSchemasText] = useState("dbo");
  const [allSchemas, setAllSchemas] = useState(false);
  const [logStartDate, setLogStartDate] = useState("");
  const [logEndDate, setLogEndDate] = useState("");
  const [maxLogRows, setMaxLogRows] = useState(10000);
  const [buildReport, setBuildReport] = useState(true);
  const [autoLoadReport, setAutoLoadReport] = useState(true);
  const [testing, setTesting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [snap, setSnap] = useState<LiveIngestionSnapshot | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const pendingAutoLoadRef = useRef<{ doLoad: boolean } | null>(null);
  const autoLoadHandledRef = useRef<string | null>(null);

  const stopWs = useCallback(() => {
    wsRef.current?.close();
    wsRef.current = null;
  }, []);

  useEffect(() => {
    return () => stopWs();
  }, [stopWs]);

  useEffect(() => {
    if (!jobId) return;
    stopWs();
    const ws = new WebSocket(liveWsUrl(jobId));
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data as string) as LiveIngestionSnapshot & { error?: string };
        if (data.error) {
          setError(data.error);
          return;
        }
        setSnap({
          stage: String(data.stage ?? ""),
          percent: Number(data.percent ?? 0),
          log_lines: Array.isArray(data.log_lines) ? (data.log_lines as string[]) : [],
          status: data.status as LiveIngestionSnapshot["status"],
          errors: Array.isArray(data.errors) ? (data.errors as string[]) : undefined,
          build_report: typeof data.build_report === "boolean" ? data.build_report : undefined,
          report_path: data.report_path ?? undefined,
          report_build_error: data.report_build_error ?? undefined
        });
      } catch {
        /* ignore */
      }
    };
    ws.onerror = () => setError("WebSocket error");
    return () => {
      ws.close();
    };
  }, [jobId, setError, stopWs]);

  useEffect(() => {
    if (!jobId || !snap) return;
    const term = snap.status === "success" || snap.status === "partial";
    if (!term) return;
    if (autoLoadHandledRef.current === jobId) return;
    const path = snap.report_path;
    if (!path || typeof path !== "string") return;
    if (!pendingAutoLoadRef.current?.doLoad) return;
    autoLoadHandledRef.current = jobId;
    pendingAutoLoadRef.current = null;
    void (async () => {
      try {
        setError("");
        const res = await api.loadReport(path);
        setReportPath(path);
        setReportId(res.report_id);
        const summary = await api.getSummary(res.report_id);
        setSummary(summary);
        setNotice("Loaded AMA report from live export.");
        navigate("/tables");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [jobId, navigate, setError, setNotice, setReportId, setReportPath, setSummary, snap]);

  async function testConnection() {
    setTesting(true);
    setError("");
    try {
      const body =
        connString.trim().length > 0
          ? { mode, connection_string: connString.trim(), encrypted: false }
          : {
              mode,
              connection_string: null,
              encrypted: false,
              host,
              port: Number(port),
              user,
              password,
              database,
              service_name: serviceName
            };
      const res = await api.testConnection(body as Parameters<typeof api.testConnection>[0]);
      if (!res.ok) {
        setError(res.error || "Connection failed");
        return;
      }
      setError("");
      alert(`OK: ${res.db_version || "connected"} (${res.tables_found} tables)`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  }

  async function startIngestion() {
    setStarting(true);
    setSnap(null);
    setError("");
    autoLoadHandledRef.current = null;
    pendingAutoLoadRef.current = { doLoad: buildReport && autoLoadReport };
    try {
      const payload: LiveStartPayload = {
        mode,
        connection_name: connectionName,
        build_report: buildReport
      };
      if (allSchemas) {
        payload.all_schemas = true;
      } else {
        const schemaList = schemasText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        if (schemaList.length > 0) payload.schemas = schemaList;
      }
      if (logStartDate.trim()) payload.log_start_date = logStartDate.trim();
      if (logEndDate.trim()) payload.log_end_date = logEndDate.trim();
      payload.max_log_rows = maxLogRows;
      if (connString.trim()) {
        payload.connection_string = connString.trim();
      } else {
        payload.host = host;
        payload.port = Number(port);
        payload.user = user;
        payload.password = password;
        payload.database = database;
        if (mode === "oracle") payload.service_name = serviceName;
      }
      const startRes = await api.startLiveIngestion(payload);
      if (buildReport && startRes.build_report !== true) {
        setError(
          "The API ignored build_report (response was not true). Rebuild and restart the Docker api service: " +
            "docker compose build api && docker compose up -d api — then hard-refresh the UI."
        );
      }
      setJobId(startRes.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  return (
    <Stack spacing={2}>
      <Alert severity="info">
        Real extraction is read-only — no DDL/DML is deployed to the target database. Table DDL and query
        logs are pulled from SQL Server and written under <code>live_data/&lt;connection_name&gt;/</code>.
      </Alert>
      <Alert severity="info" variant="outlined">
        <Typography variant="body2">
          Using <strong>Docker</strong>? The UI comes from the <code>web</code> image (<code>docker compose build web</code>
          / <code>docker compose watch</code>). The live-ingestion API (report build, <code>build_report</code>) comes from
          the <code>api</code> image — rebuild it after backend changes:{" "}
          <code>docker compose build api && docker compose up -d api</code>. For local UI iteration, run{" "}
          <code>npm run dev</code> in <code>frontend/</code>.
        </Typography>
      </Alert>
      <PageCard title="Live connection">
        <Stack spacing={2} sx={{ maxWidth: 560 }}>
          <TextField select label="Dialect" size="small" value={mode} onChange={(e) => setMode(e.target.value as (typeof MODES)[number])}>
            {MODES.map((m) => (
              <MenuItem key={m} value={m}>
                {m}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Connection name (folder under live_data)"
            size="small"
            value={connectionName}
            onChange={(e) => setConnectionName(e.target.value)}
            placeholder="e.g. prod-sqlserver-01"
          />
          {mode !== "sqlserver" ? (
            <Typography variant="caption" color="text.secondary">
              Real extraction is available for SQL Server only in this release.
            </Typography>
          ) : null}
          <Typography variant="subtitle2" color="text.secondary">
            After export — AMA report
          </Typography>
          <FormControlLabel
            control={
              <Checkbox checked={buildReport} onChange={(e) => setBuildReport(e.target.checked)} size="small" />
            }
            label="Build AMA report after export (discovery + DDL merge → JSON next to artifacts)"
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={autoLoadReport}
                disabled={!buildReport}
                onChange={(e) => setAutoLoadReport(e.target.checked)}
                size="small"
              />
            }
            label="When ready, load that report in this UI and open Tables"
          />
          <Typography variant="caption" color="text.secondary">
            Optional: paste a full connection string (not logged by the server). If set, host fields below are ignored.
          </Typography>
          <TextField
            label="Connection string (optional)"
            size="small"
            value={connString}
            onChange={(e) => setConnString(e.target.value)}
            multiline
            minRows={2}
          />
          <TextField label="Host" size="small" value={host} onChange={(e) => setHost(e.target.value)} />
          <TextField label="Port" size="small" value={port} onChange={(e) => setPort(e.target.value)} />
          <TextField label="User" size="small" value={user} onChange={(e) => setUser(e.target.value)} />
          <TextField
            label="Password"
            type="password"
            size="small"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <TextField
            label={mode === "oracle" ? "Database / default schema hint" : "Database"}
            size="small"
            value={database}
            onChange={(e) => setDatabase(e.target.value)}
          />
          {mode === "oracle" ? (
            <TextField label="Service name" size="small" value={serviceName} onChange={(e) => setServiceName(e.target.value)} />
          ) : null}
          <FormControlLabel
            control={
              <Checkbox
                checked={allSchemas}
                onChange={(e) => setAllSchemas(e.target.checked)}
              />
            }
            label="All user schemas (entire database)"
          />
          <TextField
            label="Schemas (comma-separated)"
            size="small"
            value={schemasText}
            onChange={(e) => setSchemasText(e.target.value)}
            disabled={allSchemas}
            helperText={
              allSchemas
                ? "Exports every BASE TABLE in the database (excludes sys / INFORMATION_SCHEMA)."
                : "Defaults to dbo when empty; list schemas such as dbo, finance, logistics."
            }
          />
          <TextField
            label="Log start date"
            type="date"
            size="small"
            value={logStartDate}
            onChange={(e) => setLogStartDate(e.target.value)}
            InputLabelProps={{ shrink: true }}
            helperText="Optional — last 7 days through today when omitted (Query Store only)"
          />
          <TextField
            label="Log end date"
            type="date"
            size="small"
            value={logEndDate}
            onChange={(e) => setLogEndDate(e.target.value)}
            InputLabelProps={{ shrink: true }}
          />
          <TextField
            label="Max log rows"
            type="number"
            size="small"
            value={maxLogRows}
            onChange={(e) => setMaxLogRows(Number(e.target.value || 10000))}
            inputProps={{ min: 1, max: 50000 }}
          />
          <Stack direction="row" spacing={1}>
            <Button variant="outlined" disabled={testing} onClick={() => void testConnection()}>
              {testing ? "Testing…" : "Test connection"}
            </Button>
            <Button variant="contained" disabled={starting} onClick={() => void startIngestion()}>
              {starting ? "Starting…" : "Start ingestion"}
            </Button>
          </Stack>
        </Stack>
      </PageCard>
      <PageCard title="Ingestion progress">
        {snap ? (
          <Stack spacing={1}>
            <Typography variant="body2">
              Status: <strong>{snap.status}</strong> — {snap.stage}
            </Typography>
            <LinearProgress variant="determinate" value={Math.min(100, Math.max(0, snap.percent))} sx={{ height: 8, borderRadius: 1 }} />
            <Typography variant="caption" color="text.secondary">
              {snap.percent}%
            </Typography>
            {snap.errors && snap.errors.length > 0 ? (
              <Alert severity="error">
                {snap.errors.map((x, i) => (
                  <div key={i}>{x}</div>
                ))}
              </Alert>
            ) : null}
            {snap.report_path ? (
              <Typography variant="body2" color="text.secondary">
                Report file: <code>{snap.report_path}</code>
              </Typography>
            ) : null}
            {snap.report_build_error ? (
              <Alert severity="warning">{snap.report_build_error}</Alert>
            ) : null}
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1,
                maxHeight: 240,
                overflow: "auto",
                bgcolor: "#0f172a",
                color: "#e2e8f0",
                fontSize: 11,
                borderRadius: 1
              }}
            >
              {(snap.log_lines || []).join("\n")}
            </Box>
          </Stack>
        ) : (
          <Typography variant="body2" color="text.secondary">
            Start an ingestion job to see WebSocket-driven progress.
          </Typography>
        )}
      </PageCard>
    </Stack>
  );
}
