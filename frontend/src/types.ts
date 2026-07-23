export type ReportSummary = {
  report_id: string;
  table_count: number;
  domains: string[];
  migration_context: string;
  lineage_edge_count: number;
  has_glossary: boolean;
  pending_review_count?: number;
  rejected_mapping_count?: number;
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

export type LineageNodeRole = "center" | "neighbor" | "broken";

export type LineageFlowNode = {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: { label: string; role: LineageNodeRole; query_count?: number | null };
};

export type LineageFlowEdge = {
  id: string;
  source: string;
  target: string;
  label?: string;
  data?: {
    weight?: number;
    kind: string;
    column?: string;
    coquery_count?: number;
  };
};

export type LineageSubgraphResponse = {
  nodes: LineageFlowNode[];
  edges: LineageFlowEdge[];
  empty_reason: string | null;
  center_table_key: string;
  lineage_mode?: "pk_fk" | "coquery";
  legend?: string;
};

export type ConnectionTestResponse = {
  ok: boolean;
  mode: string;
  db_version: string | null;
  tables_found: number;
  sample_tables: string[];
  error: string | null;
};

export type LiveJobStatus = "queued" | "running" | "success" | "partial" | "failure";

export type LiveIngestionSnapshot = {
  status: LiveJobStatus;
  stage: string;
  percent: number;
  log_lines: string[];
  errors?: string[];
  connection_name?: string;
  error?: string;
  /** Echo of the job's ``build_report`` request flag. */
  build_report?: boolean | null;
  /** Server path to generated JSON report when ``build_report`` was requested. */
  report_path?: string | null;
  report_build_error?: string | null;
};
