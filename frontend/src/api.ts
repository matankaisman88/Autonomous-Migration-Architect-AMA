import type { AgentTurnResponse, BulkJob, ReportSummary, ScoredTable } from "./types";

const API_BASE = import.meta.env.VITE_AMA_API_BASE ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

export const api = {
  base: API_BASE,
  loadReport: (path: string) =>
    request<{ report_id: string; table_count: number; domains: string[] }>("/report/load", {
      method: "POST",
      body: JSON.stringify({ path })
    }),
  getSummary: (reportId: string) => request<ReportSummary>(`/report/${reportId}/summary`),
  evaluate: (reportId: string) =>
    request<{
      would_migrate: number;
      would_flag_review: number;
      would_block: number;
      threshold_used: { conf_floor: number; crit_ceil: number };
      contract_preview: {
        rules: string[];
        contract_id: string;
        excluded: string[];
        table_count: number;
      };
      scored_tables: ScoredTable[];
    }>(`/scale/${reportId}/evaluate`, { method: "POST", body: JSON.stringify({}) }),
  explain: (reportId: string, tableKey: string) =>
    request<Record<string, unknown>>(`/scale/${reportId}/explain/${encodeURIComponent(tableKey)}`),
  propose: (reportId: string, tableKey: string, dialect = "duckdb") =>
    request<Record<string, unknown>>(`/migration/${reportId}/propose`, {
      method: "POST",
      body: JSON.stringify({ table_key: tableKey, dialect })
    }),
  approve: (reportId: string, payload: { model_name: string; sql: string; schema_yml: string; table_key: string; approved_by?: string }) =>
    request<Record<string, unknown>>(`/migration/${reportId}/approve`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  startBulk: (reportId: string, tableKeys: string[], dialect = "duckdb") =>
    request<{ job_id: string; queued: number; skipped: { table_key: string; reason: string }[] }>(
      `/bulk/${reportId}/start`,
      { method: "POST", body: JSON.stringify({ table_keys: tableKeys, dialect }) }
    ),
  getBulkStatus: (jobId: string) => request<BulkJob>(`/bulk/job/${jobId}`),
  clearBulkJob: (jobId: string) =>
    request<{ cleared: boolean }>(`/bulk/job/${jobId}`, {
      method: "DELETE"
    }),
  agentTurn: (
    reportId: string,
    userMessage: string,
    state: Record<string, unknown>,
    options?: {
      pending_write_action?: "approve" | "reject";
      pending_write_sql?: string;
      pending_write_schema_yml?: string;
    }
  ) =>
    request<AgentTurnResponse>(`/agent/${reportId}/turn`, {
      method: "POST",
      body: JSON.stringify({ user_message: userMessage, state, dialect: "duckdb", ...(options ?? {}) })
    }),
  planWaves: (reportId: string) =>
    request<Record<string, unknown>>(`/planner/${reportId}/waves`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  getHitl: (reportId: string) => request<Record<string, unknown>>(`/hitl/${reportId}`),
  applyHitl: (reportId: string) =>
    request<Record<string, unknown>>(`/hitl/${reportId}/apply`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  runDq: (reportId: string) =>
    request<Record<string, unknown>>(`/dq/${reportId}/run`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  impactScatter: (reportId: string) => request<Record<string, unknown>>(`/analytics/${reportId}/impact-scatter`),
  glossary: (reportId: string, confMin = 0, portfolio = "All", domains = "") =>
    request<{ entries: Record<string, unknown>[]; counts: Record<string, number> }>(
      `/analytics/${reportId}/glossary?conf_min=${encodeURIComponent(String(confMin))}&portfolio=${encodeURIComponent(
        portfolio
      )}&domains=${encodeURIComponent(domains)}`
    ),
  startCheckpointA: (reportId: string) =>
    request<Record<string, unknown>>(`/cockpit/${reportId}/checkpoint-a/start`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  pollCheckpointA: (jobId: string) => request<Record<string, unknown>>(`/cockpit/checkpoint-a/job/${jobId}`)
};

export function bulkWsUrl(jobId: string): string {
  const wsBase = API_BASE.replace(/^http/i, "ws");
  return `${wsBase}/ws/bulk/${jobId}`;
}
