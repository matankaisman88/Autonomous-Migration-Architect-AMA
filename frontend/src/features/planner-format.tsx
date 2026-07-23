import { Box, List, ListItem, ListItemText, Typography } from "@mui/material";
import type { ReactNode } from "react";

/** Render inline `**bold**` and `` `code` `` from planner rationale strings. */
export function renderInlinePlannerText(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(
        <Box component="strong" key={key++} sx={{ fontWeight: 600, color: "text.primary" }}>
          {token.slice(2, -2)}
        </Box>
      );
    } else {
      parts.push(
        <Box
          component="code"
          key={key++}
          sx={{
            px: 0.5,
            py: 0.1,
            borderRadius: 0.5,
            bgcolor: "action.hover",
            fontFamily: "monospace",
            fontSize: "0.85em"
          }}
        >
          {token.slice(1, -1)}
        </Box>
      );
    }
    last = match.index + token.length;
  }
  if (last < text.length) {
    parts.push(text.slice(last));
  }
  return parts;
}

/** Split long rationale prose into readable sentence groups. */
function rationaleBullets(text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const byMajor = trimmed.split(/\.\s+(?=\*\*[A-Z])/);
  if (byMajor.length > 1) {
    return byMajor.map((s) => (s.endsWith(".") ? s : `${s}.`)).filter(Boolean);
  }
  if (trimmed.length <= 220) return [trimmed];
  return trimmed
    .split(/\.\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (s.endsWith(".") ? s : `${s}.`));
}

export function PlannerRationale({ title, text }: { title: string; text: string }) {
  const bullets = rationaleBullets(text);
  return (
    <Box
      sx={{
        p: 1.5,
        borderRadius: 1.5,
        border: "1px solid",
        borderColor: "divider",
        bgcolor: "background.paper",
        height: "100%"
      }}
    >
      <Typography variant="subtitle2" sx={{ mb: 1, color: "text.secondary", textTransform: "uppercase", letterSpacing: "0.04em", fontSize: "0.72rem" }}>
        {title}
      </Typography>
      {bullets.length <= 1 ? (
        <Typography variant="body2" component="div" sx={{ lineHeight: 1.65, color: "text.primary" }}>
          {renderInlinePlannerText(bullets[0] ?? text)}
        </Typography>
      ) : (
        <List dense disablePadding sx={{ listStyle: "disc", pl: 2.2 }}>
          {bullets.map((bullet, idx) => (
            <ListItem key={idx} disableGutters sx={{ display: "list-item", py: 0.35 }}>
              <ListItemText
                primary={
                  <Typography variant="body2" component="span" sx={{ lineHeight: 1.6 }}>
                    {renderInlinePlannerText(bullet)}
                  </Typography>
                }
              />
            </ListItem>
          ))}
        </List>
      )}
    </Box>
  );
}
