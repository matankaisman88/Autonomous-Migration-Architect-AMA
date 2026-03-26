export type ReportSummary = {
  report_id: string;
  table_count: number;
  domains: string[];
  migration_context: string;
  lineage_edge_count: number;
  has_glossary: boolean;
};

export type ScoredTable = {
  table_key: string;
  queue: "green" | "yellow" | "red";
  confidence: number;
  criticality: number;
  business_domain: string;
  confidence_reason?: string;
  criticality_reason?: string;
};

export type BulkJob = {
  job_id: string;
  status: "queued" | "running" | "done" | "failed";
  total: number;
  completed: number;
  current_table: string;
  success: string[];
  failed: { table_key: string; reason: string }[];
  error: string;
};

export type AgentTurnResponse = {
  status: string;
  message: string;
  state: Record<string, unknown>;
  pending_write: Record<string, unknown> | null;
  tokens_used: number;
  cost_est: number;
};
