import { Box, Card, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

export function StatCard({
  label,
  value,
  subtitle,
  icon,
  accentColor = "primary.main"
}: {
  label: string;
  value: string | number;
  subtitle?: string;
  icon?: ReactNode;
  accentColor?: string;
}) {
  return (
    <Card
      sx={{
        borderTop: "2px solid",
        borderColor: accentColor,
        p: 2,
        height: "100%",
        background: "linear-gradient(135deg, rgba(30,41,59,1) 0%, rgba(15,23,42,0.6) 100%)"
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Typography variant="subtitle2" color="text.secondary">
            {label}
          </Typography>
          <Typography variant="h5" sx={{ my: 0.5 }}>
            {value}
          </Typography>
          {subtitle && <Typography variant="caption">{subtitle}</Typography>}
        </Box>
        {icon && <Box sx={{ opacity: 0.7 }}>{icon}</Box>}
      </Stack>
    </Card>
  );
}

