import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { ReportSummary } from "./types";

type AppState = {
  reportId: string;
  setReportId: (v: string) => void;
  reportPath: string;
  setReportPath: (v: string) => void;
  summary: ReportSummary | null;
  setSummary: (v: ReportSummary | null) => void;
  error: string;
  setError: (v: string) => void;
  notice: string;
  setNotice: (v: string) => void;
};

const DEFAULT_REPORT =
  import.meta.env.VITE_DEFAULT_REPORT_PATH ??
  "/app/sample_data/scale_engine_chaos/chaos_report.json";

const Ctx = createContext<AppState | null>(null);

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [reportId, setReportId] = useState("");
  const [reportPath, setReportPath] = useState(DEFAULT_REPORT);
  const [summary, setSummary] = useState<ReportSummary | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const value = useMemo(
    () => ({
      reportId,
      setReportId,
      reportPath,
      setReportPath,
      summary,
      setSummary,
      error,
      setError,
      notice,
      setNotice
    }),
    [reportId, reportPath, summary, error, notice]
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAppState(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAppState must be used within AppStateProvider");
  return ctx;
}
