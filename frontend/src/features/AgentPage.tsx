import SendIcon from "@mui/icons-material/Send";
import { Avatar, Button, Chip, Grid2, List, ListItem, ListItemAvatar, ListItemText, Stack, TextField, Typography } from "@mui/material";
import { useEffect, useState } from "react";
import { api } from "../api";
import { PageCard } from "../components/PageCard";
import { useRequireReportId, useErrorSetter } from "./common";
import type { AgentTurnResponse } from "../types";

export function AgentPage() {
  const reportId = useRequireReportId();
  const setError = useErrorSetter();
  const [prompt, setPrompt] = useState("Show Status");
  const [agentState, setAgentState] = useState<Record<string, unknown>>({ messages: [] });
  const [result, setResult] = useState<AgentTurnResponse | null>(null);
  const [pendingSql, setPendingSql] = useState("");
  const [pendingSchema, setPendingSchema] = useState("");
  const [pendingModelName, setPendingModelName] = useState("");
  const messages = (result?.state?.messages as { role?: string; content?: string }[] | undefined) ?? [];
  const pendingWrite = (result?.pending_write as Record<string, unknown> | null) ?? null;

  useEffect(() => {
    if (!pendingWrite) return;
    setPendingModelName(String(pendingWrite.model_name ?? ""));
    setPendingSql(String(pendingWrite.sql ?? ""));
    setPendingSchema(String(pendingWrite.schema_yml ?? ""));
  }, [pendingWrite]);

  return (
    <Grid2 container spacing={2}>
      <Grid2 size={{ xs: 12, md: 4 }}>
        <PageCard title="Agent Input">
          <Stack spacing={1.5}>
            <TextField value={prompt} onChange={(e) => setPrompt(e.target.value)} multiline minRows={3} label="Message" />
            <Button
              variant="contained"
              startIcon={<SendIcon />}
              disabled={!reportId}
              onClick={async () => {
                try {
                  const turn = await api.agentTurn(reportId, prompt, agentState);
                  setResult(turn);
                  setAgentState(turn.state);
                } catch (e) {
                  setError(e);
                }
              }}
            >
              Send Turn
            </Button>
            <Typography variant="caption">Stateless mode: client owns conversation state.</Typography>
            {pendingWrite && (
              <Stack spacing={1} sx={{ mt: 1.5, p: 1.2, border: "1px solid #e2e8f0", borderRadius: 1 }}>
                <Typography variant="subtitle2">Pending Write Approval</Typography>
                <Typography variant="caption" color="text.secondary">
                  Model: {pendingModelName || "-"}
                </Typography>
                <TextField
                  label="SQL (editable)"
                  value={pendingSql}
                  onChange={(e) => setPendingSql(e.target.value)}
                  multiline
                  minRows={6}
                />
                <TextField
                  label="Schema YAML (editable)"
                  value={pendingSchema}
                  onChange={(e) => setPendingSchema(e.target.value)}
                  multiline
                  minRows={3}
                />
                <Stack direction="row" spacing={1}>
                  <Button
                    color="success"
                    variant="contained"
                    onClick={async () => {
                      try {
                        const turn = await api.agentTurn(reportId, "approve pending write", agentState, {
                          pending_write_action: "approve",
                          pending_write_sql: pendingSql,
                          pending_write_schema_yml: pendingSchema
                        });
                        setResult(turn);
                        setAgentState(turn.state);
                      } catch (e) {
                        setError(e);
                      }
                    }}
                  >
                    Approve & Write
                  </Button>
                  <Button
                    color="inherit"
                    variant="outlined"
                    onClick={async () => {
                      try {
                        const turn = await api.agentTurn(reportId, "reject pending write", agentState, {
                          pending_write_action: "reject"
                        });
                        setResult(turn);
                        setAgentState(turn.state);
                      } catch (e) {
                        setError(e);
                      }
                    }}
                  >
                    Reject
                  </Button>
                </Stack>
              </Stack>
            )}
          </Stack>
        </PageCard>
      </Grid2>
      <Grid2 size={{ xs: 12, md: 8 }}>
        <PageCard title="Agent Output">
          {!result ? (
            <Typography variant="body2" color="text.secondary">
              Send a turn to see agent output.
            </Typography>
          ) : (
            <Stack spacing={1}>
              <Stack direction="row" spacing={1}>
                <Chip label={`Status: ${result.status}`} color="primary" />
                <Chip label={`Tokens: ${result.tokens_used}`} variant="outlined" />
                <Chip label={`Cost: ${result.cost_est.toFixed(4)}`} variant="outlined" />
              </Stack>
              <Typography variant="body2">{result.message}</Typography>
              <List dense sx={{ maxHeight: 280, overflow: "auto", border: "1px solid #e2e8f0", borderRadius: 1 }}>
                {messages.slice(-8).map((m, idx) => (
                  <ListItem key={`${idx}-${m.role}`}>
                    <ListItemAvatar>
                      <Avatar sx={{ width: 28, height: 28 }}>{String(m.role || "?").slice(0, 1).toUpperCase()}</Avatar>
                    </ListItemAvatar>
                    <ListItemText primary={m.role} secondary={String(m.content || "")} />
                  </ListItem>
                ))}
              </List>
            </Stack>
          )}
        </PageCard>
      </Grid2>
    </Grid2>
  );
}

