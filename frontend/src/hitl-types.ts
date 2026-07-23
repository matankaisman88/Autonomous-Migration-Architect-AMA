export type HitlReviewRow = {
  source_table?: string;
  legacy_name?: string;
  suggested_ddl?: string;
  merge_confidence?: number;
  citation?: string;
  strategy?: string;
  category?: string;
};

export type HitlQueueItem = {
  signature: string;
  row: HitlReviewRow;
  decision: string | null;
  status: "pending" | "approved" | "rejected";
  merge_confidence: number;
};

export type HitlQueueResponse = {
  items: HitlQueueItem[];
  rejected_items?: HitlQueueItem[];
  pending_count: number;
  approved_count: number;
  rejected_count: number;
  counts: {
    merged_entities: number;
    review_candidates: number;
    trash_candidates: number;
  };
};

export type HitlDecideResponse = {
  signature: string;
  action: string;
  saved: boolean;
  applied?: boolean;
  counts?: HitlQueueResponse["counts"];
  pending_count?: number;
};

export type HitlBatchDecideResponse = {
  matched: number;
  action: string;
  saved: boolean;
  applied?: boolean;
  counts?: HitlQueueResponse["counts"];
  pending_count?: number;
};
