import { Card, CardContent, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

export function StatCard({
  label,
  value,
  icon,
  subtitle
}: {
  label: string;
  value: string | number;
  icon?: ReactNode;
  subtitle?: string;
}) {
  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Stack spacing={0.4}>
            <Typography variant="caption" color="text.secondary">
              {label}
            </Typography>
            <Typography variant="h5">{value}</Typography>
            {subtitle && (
              <Typography variant="caption" color="text.secondary">
                {subtitle}
              </Typography>
            )}
          </Stack>
          {icon}
        </Stack>
      </CardContent>
    </Card>
  );
}

