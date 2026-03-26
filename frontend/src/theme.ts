import { createTheme } from "@mui/material/styles";

export const appTheme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#0f172a" },
    secondary: { main: "#0ea5e9" },
    background: { default: "#f8fafc", paper: "#ffffff" }
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: "Inter, system-ui, Arial, sans-serif",
    h5: { fontWeight: 700 },
    h6: { fontWeight: 700 }
  },
  components: {
    MuiCard: {
      styleOverrides: {
        root: {
          border: "1px solid #e2e8f0",
          boxShadow: "none"
        }
      }
    }
  }
});

