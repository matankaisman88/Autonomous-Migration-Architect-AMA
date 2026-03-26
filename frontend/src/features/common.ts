import { useAppState } from "../state";
import { useCallback } from "react";

export function useRequireReportId(): string {
  const { reportId } = useAppState();
  return reportId;
}

export function useErrorSetter(): (e: unknown) => void {
  const { setError } = useAppState();
  return useCallback((e: unknown) => setError(e instanceof Error ? e.message : String(e)), [setError]);
}

