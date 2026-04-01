import { NavLink, Outlet } from "react-router-dom";
import { api } from "./api";
import { useAppState } from "./state";
import {
  AppBar,
  Chip,
  Box,
  Button,
  Container,
  Drawer,
  List,
  ListItemButton,
  ListItemText,
  Snackbar,
  Stack,
  TextField,
  Toolbar,
  Typography,
  Alert
} from "@mui/material";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/tables", label: "Tables" },
  { to: "/live", label: "Live connection" },
  { to: "/glossary", label: "Glossary" },
  { to: "/bulk", label: "Bulk" },
  { to: "/planner", label: "Planner" },
  { to: "/hitl", label: "HITL" },
  { to: "/dq", label: "Data Quality" },
  { to: "/cockpit", label: "DBT Cockpit" },
  { to: "/agent", label: "Agent" }
];

export function AppShell() {
  const { reportId, reportPath, setReportPath, setReportId, summary, setSummary, error, setError, notice, setNotice } =
    useAppState();
  const drawerWidth = 240;

  async function loadReport() {
    setError("");
    setNotice("");
    try {
      const res = await api.loadReport(reportPath);
      setReportId(res.report_id);
      const sum = await api.getSummary(res.report_id);
      setSummary(sum);
      setNotice(`Report loaded: ${res.report_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Box sx={{ display: "flex", minHeight: "100vh", bgcolor: "background.default" }}>
      <AppBar position="fixed" color="inherit" elevation={0}>
        <Toolbar sx={{ gap: 2, justifyContent: "space-between" }}>
          <Stack direction="row" spacing={1.2} alignItems="center" sx={{ minWidth: 180 }}>
            <Typography variant="h6" sx={{ fontWeight: 700, letterSpacing: "0.06em" }}>
              AMA
            </Typography>
            <Chip
              label={reportId || "v1.0"}
              size="small"
              sx={{
                background: "linear-gradient(135deg, #38bdf8 0%, #818cf8 100%)",
                color: "#0f172a",
                borderRadius: 1
              }}
            />
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ flexGrow: 1, maxWidth: 840 }}>
            <Typography variant="subtitle2" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
              Load Report
            </Typography>
            <TextField
              fullWidth
              value={reportPath}
              onChange={(e) => setReportPath(e.target.value)}
              label="Report path"
            />
            <Button variant="contained" onClick={loadReport}>
              Load
            </Button>
          </Stack>
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: drawerWidth,
          flexShrink: 0,
          [`& .MuiDrawer-paper`]: { width: drawerWidth, boxSizing: "border-box", mt: 8 }
        }}
      >
        <List>
          {NAV.map((item) => (
            <ListItemButton
              key={item.to}
              component={NavLink}
              to={item.to}
              sx={{
                borderLeft: "3px solid transparent",
                "&.active": {
                  borderLeft: "3px solid",
                  borderColor: "primary.main",
                  backgroundColor: "rgba(56,189,248,0.06)",
                  color: "primary.main"
                },
                py: 0.8,
                px: 2
              }}
            >
              <ListItemText primary={item.label} />
            </ListItemButton>
          ))}
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, mt: 10, ml: 2, mr: 2 }}>
        <Container maxWidth={false} sx={{ px: { xs: 1, md: 2 } }}>
          <Stack spacing={2} sx={{ mb: 2 }}>
            {summary && (
              <Typography variant="body2" color="text.secondary">
                Tables: {summary.table_count} | Domains: {summary.domains.join(", ")}
              </Typography>
            )}
          </Stack>
          <Outlet />
        </Container>
      </Box>

      <Snackbar open={Boolean(notice)} autoHideDuration={2500} onClose={() => setNotice("")}>
        <Alert severity="success" onClose={() => setNotice("")} variant="filled">
          {notice}
        </Alert>
      </Snackbar>
      <Snackbar open={Boolean(error)} autoHideDuration={5000} onClose={() => setError("")}>
        <Alert severity="error" onClose={() => setError("")} variant="filled">
          {error}
        </Alert>
      </Snackbar>
    </Box>
  );
}
