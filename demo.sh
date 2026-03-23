#!/usr/bin/env bash
# Full Kfar Supply quickstart: generate sample data → ingest → export-plan (Jira + Confluence).
# Run from the repository root after `pip install -e .`:
#   bash demo.sh
# With a domain sandbox from tools/generate_domain_data.py:
#   bash demo.sh --sandbox out/sandbox_hr_<timestamp>

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SANDBOX="${SANDBOX:-sample_data/kfar_supply}"
# Parse --sandbox argument
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sandbox) SANDBOX="$2"; shift 2 ;;
    *) shift ;;
  esac
done

REPORT="${ROOT}/kfar_report.json"
JIRA_OUT="${ROOT}/kfar_export_jira.json"
CONF_OUT="${ROOT}/kfar_export_confluence.html"

if [[ "${SANDBOX}" == "sample_data/kfar_supply" ]]; then
  echo "==> [1/4] Generating Kfar Supply sample data..."
  python tools/generate_kfar_supply.py
else
  echo "==> [1/4] Using sandbox ${SANDBOX} (skipping Kfar generator)..."
fi

if [[ -f "${SANDBOX}/ddl/manifest.json" ]]; then
  DDL_MANIFEST="${SANDBOX}/ddl/manifest.json"
elif [[ -f "${SANDBOX}/ddl/kfar_manifest.json" ]]; then
  DDL_MANIFEST="${SANDBOX}/ddl/kfar_manifest.json"
else
  echo "error: no manifest.json or kfar_manifest.json under ${SANDBOX}/ddl" >&2
  exit 1
fi

echo "==> [2/4] Ingesting SQL logs (discovery mode)..."
ama-ingest run \
  --data-root . \
  --sql-logs "${SANDBOX}/sql_logs/*.jsonl" \
  --ddl-manifest "${DDL_MANIFEST}" \
  --glossary "${SANDBOX}/glossary/*_glossary.json" \
  --glossary-dirty "${SANDBOX}/glossary/*_glossary_dirty.json" \
  --comms-dir "${SANDBOX}/comms" \
  --git-sql-roots "${SANDBOX}/git_sql" \
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
