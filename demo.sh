#!/usr/bin/env bash
# Full Kfar Supply quickstart: generate sample data → ingest → export-plan (Jira + Confluence).
# Run from the repository root after `pip install -e .`:
#   bash demo.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

REPORT="${ROOT}/kfar_report.json"
JIRA_OUT="${ROOT}/kfar_export_jira.json"
CONF_OUT="${ROOT}/kfar_export_confluence.html"

echo "==> [1/4] Generating Kfar Supply sample data..."
python tools/generate_kfar_supply.py

echo "==> [2/4] Ingesting SQL logs (discovery mode)..."
ama-ingest run \
  --data-root . \
  --sql-logs "sample_data/kfar_supply/sql_logs/*.jsonl" \
  --ddl-manifest sample_data/kfar_supply/ddl/kfar_manifest.json \
  --glossary sample_data/kfar_supply/glossary/kfar_glossary.json \
  --glossary-dirty sample_data/kfar_supply/glossary/kfar_glossary_dirty.json \
  --comms-dir sample_data/kfar_supply/comms \
  --git-sql-roots sample_data/kfar_supply/git_sql \
  --target-schema dbo \
  --target-table orders \
  --discovery-mode --discovery-merge-all \
  --format json \
  -o "${REPORT}"

echo "==> [3/4] Export plan (Jira bulk-create JSON)..."
ama-ingest export-plan --report "${REPORT}" --format jira --out "${JIRA_OUT}"

echo "==> [4/4] Export plan (Confluence HTML)..."
ama-ingest export-plan --report "${REPORT}" --format confluence --out "${CONF_OUT}"

echo ""
echo "Demo outputs (absolute paths):"
echo "  Report JSON:          ${REPORT}"
echo "  Jira export:          ${JIRA_OUT}"
echo "  Confluence export:    ${CONF_OUT}"
