import { createTheme, alpha } from "@mui/material/styles";

export const appTheme = createTheme({
  palette: {
    mode: "dark",
    primary: { main: "#38bdf8" },
    secondary: { main: "#818cf8" },
    success: { main: "#34d399" },
    warning: { main: "#fbbf24" },
    error: { main: "#f87171" },
    background: { default: "#0f172a", paper: "#1e293b" },
    text: { primary: "#f1f5f9", secondary: "#94a3b8" },
    divider: "rgba(148,163,184,0.12)"
  },
  shape: { borderRadius: 8 },
  typography: {
    fontFamily: "'IBM Plex Mono', 'JetBrains Mono', 'Fira Code', monospace",
    h5: { fontWeight: 700, letterSpacing: "-0.02em" },
    h6: { fontWeight: 700, letterSpacing: "-0.01em" },
    subtitle2: {
      fontWeight: 600,
      letterSpacing: "0.04em",
      textTransform: "uppercase",
      fontSize: "0.7rem"
    },
    body2: { fontSize: "0.82rem" },
    caption: { fontSize: "0.72rem", color: "#64748b" }
  },
  components: {
    MuiCard: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
          border: `1px solid ${alpha("#94a3b8", 0.1)}`,
          boxShadow: "none"
        }
      }
    },
    MuiChip: {
      styleOverrides: {
        root: { fontFamily: "inherit", fontWeight: 600, fontSize: "0.7rem", height: 22 }
      }
    },
    MuiButton: {
      styleOverrides: {
        root: { fontFamily: "inherit", fontWeight: 600, letterSpacing: "0.04em", textTransform: "none" },
        contained: {
          background: "linear-gradient(135deg, #38bdf8 0%, #818cf8 100%)",
          color: "#0f172a",
          "&:hover": { background: "linear-gradient(135deg, #7dd3fc 0%, #a5b4fc 100%)" }
        }
      }
    },
    MuiTableCell: {
      styleOverrides: {
        head: {
          fontWeight: 700,
          fontSize: "0.72rem",
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "#64748b",
          borderBottom: "1px solid rgba(148,163,184,0.12)"
        },
        body: {
          fontSize: "0.82rem",
          borderBottom: "1px solid rgba(148,163,184,0.06)"
        }
      }
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: {
          borderRadius: 4,
          height: 6,
          backgroundColor: "rgba(148,163,184,0.12)"
        },
        bar: {
          borderRadius: 4,
          background: "linear-gradient(90deg, #38bdf8, #34d399)"
        }
      }
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          backgroundColor: "#0f172a",
          borderRight: "1px solid rgba(148,163,184,0.08)"
        }
      }
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: "#0f172a",
          borderBottom: "1px solid rgba(148,163,184,0.08)"
        }
      }
    },
    MuiTextField: {
      defaultProps: { size: "small" }
    }
  }
});

