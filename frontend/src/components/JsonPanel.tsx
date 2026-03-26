import { Box } from "@mui/material";

export function JsonPanel({ data }: { data: unknown }) {
  if (!data) return null;
  return (
    <Box
      component="pre"
      sx={{
        p: 2,
        borderRadius: 1.5,
        overflow: "auto",
        background: "#0f172a",
        color: "#e2e8f0",
        fontSize: 12,
        mt: 1
      }}
    >
      {JSON.stringify(data, null, 2)}
    </Box>
  );
}

