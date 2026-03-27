import { Chip } from "@mui/material";
import { useAppState } from "../state";
import { createElement, useCallback } from "react";

export function useRequireReportId(): string {
  const { reportId } = useAppState();
  return reportId;
}

export function useErrorSetter(): (e: unknown) => void {
  const { setError } = useAppState();
  return useCallback((e: unknown) => setError(e instanceof Error ? e.message : String(e)), [setError]);
}

export function QueueChip({ queue }: { queue: string }) {
  const map = {
    green: { color: "success" as const, label: "🟢 BULK" },
    yellow: { color: "warning" as const, label: "🟡 REVIEW" },
    red: { color: "error" as const, label: "🔴 BLOCKED" }
  };
  const { color, label } = map[queue as keyof typeof map] ?? { color: "default" as const, label: queue };
  return createElement(Chip, { color, label, size: "small" });
}

