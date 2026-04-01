import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AppShell } from "./app-shell";
import { AgentPage } from "./features/AgentPage";
import { BulkPage } from "./features/BulkPage";
import { CockpitPage } from "./features/CockpitPage";
import { DqPage } from "./features/DqPage";
import { HitlPage } from "./features/HitlPage";
import { GlossaryPage } from "./features/GlossaryPage";
import { OverviewPage } from "./features/OverviewPage";
import { PlannerPage } from "./features/PlannerPage";
import { LiveConnectionPage } from "./features/LiveConnectionPage";
import { TablesPage } from "./features/TablesPage";
import { AppStateProvider } from "./state";

export default function App() {
  return (
    <AppStateProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="tables" element={<TablesPage />} />
            <Route path="live" element={<LiveConnectionPage />} />
            <Route path="bulk" element={<BulkPage />} />
            <Route path="planner" element={<PlannerPage />} />
            <Route path="glossary" element={<GlossaryPage />} />
            <Route path="hitl" element={<HitlPage />} />
            <Route path="dq" element={<DqPage />} />
            <Route path="cockpit" element={<CockpitPage />} />
            <Route path="agent" element={<AgentPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </AppStateProvider>
  );
}
