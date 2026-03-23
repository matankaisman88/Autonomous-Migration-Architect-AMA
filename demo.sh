#!/usr/bin/env bash
# Full Kfar Supply quickstart: generate sample data → ingest → export-plan (Jira CSV + Confluence).
# Run from the repository root after `pip install -e .`:
#   bash demo.sh
# Multi-domain (one command — generates sandbox then runs pipeline):
#   bash demo.sh --domain hr
#   bash demo.sh --domain finance --lines 8000 --seed 99
# Use an existing sandbox only:
#   bash demo.sh --sandbox out/sandbox_hr_YYYYMMDD_HHMMSS

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SANDBOX="${SANDBOX:-sample_data/kfar_supply}"
DOMAIN=""
DOMAIN_LINES="${DOMAIN_LINES:-10000}"
DOMAIN_SEED="${DOMAIN_SEED:-42}"
SANDBOX_FROM_ARG=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sandbox)
      SANDBOX="$2"
      SANDBOX_FROM_ARG=1
      shift 2
      ;;
    --domain)
      DOMAIN="$2"
      shift 2
      ;;
    --lines)
      DOMAIN_LINES="$2"
      shift 2
      ;;
    --seed)
      DOMAIN_SEED="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: bash demo.sh" >&2
      echo "       bash demo.sh --domain hr [--lines N] [--seed N]" >&2
      echo "       bash demo.sh --sandbox PATH" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$DOMAIN" && "$SANDBOX_FROM_ARG" -eq 1 ]]; then
  echo "error: use either --domain or --sandbox, not both." >&2
  exit 1
fi

if [[ -n "$DOMAIN" ]]; then
  echo "==> [1/4] Generating ${DOMAIN} domain sandbox (${DOMAIN_LINES} lines, seed ${DOMAIN_SEED})..."
  SANDBOX="$(python tools/generate_domain_data.py \
    --domain "${DOMAIN}" \
    --lines "${DOMAIN_LINES}" \
    --seed "${DOMAIN_SEED}" \
    --out-dir out \
    --print-path-only)"
  echo "    Sandbox: ${SANDBOX}"
elif [[ "${SANDBOX}" == "sample_data/kfar_supply" ]]; then
  echo "==> [1/4] Generating Kfar Supply sample data..."
  python tools/generate_kfar_supply.py
else
  echo "==> [1/4] Using sandbox ${SANDBOX} (skipping generators)..."
fi

# Resolve sandbox to absolute path (Git Bash / WSL: relative "out/..." or /c/.../out/...).
if [[ "${SANDBOX}" != "sample_data/kfar_supply" ]]; then
  _sb="${SANDBOX}"
  if [[ -d "${_sb}" ]]; then
    SANDBOX="$(cd "${_sb}" && pwd)"
  elif [[ -d "${ROOT}/${SANDBOX}" ]]; then
    SANDBOX="$(cd "${ROOT}/${SANDBOX}" && pwd)"
  else
    echo "error: sandbox directory not found: ${SANDBOX}" >&2
    echo "  Regenerate with: python tools/generate_domain_data.py --domain ${DOMAIN:-hr} ..." >&2
    exit 1
  fi
fi

if [[ ! -d "${SANDBOX}/ddl" ]]; then
  echo "error: missing ${SANDBOX}/ddl (incomplete or stale sandbox)." >&2
  echo "  Regenerate with: bash demo.sh --domain ${DOMAIN:-hr}" >&2
  exit 1
fi
if [[ -f "${SANDBOX}/ddl/manifest.json" ]]; then
  _DDL_NAME="manifest.json"
elif [[ -f "${SANDBOX}/ddl/kfar_manifest.json" ]]; then
  _DDL_NAME="kfar_manifest.json"
else
  echo "error: no manifest.json or kfar_manifest.json under ${SANDBOX}/ddl" >&2
  exit 1
fi

# Paths relative to --data-root (.) so Python on Windows expands globs correctly.
# Git Bash sets SANDBOX to /c/... which breaks glob.glob (e.g. C:\c\...).
if [[ "${SANDBOX}" == "sample_data/kfar_supply" ]]; then
  SANDBOX_REL="sample_data/kfar_supply"
else
  SANDBOX_REL="$(python -c "import os,sys; r=os.path.relpath(sys.argv[1], sys.argv[2]); print(r.replace(chr(92), '/'))" "$SANDBOX" "$ROOT")"
fi
DDL_MANIFEST="${SANDBOX_REL}/ddl/${_DDL_NAME}"

# All pipeline outputs live inside the sandbox (paths relative to repo root for ama-ingest).
if [[ -n "$DOMAIN" ]]; then
  REPORT="${SANDBOX_REL}/${DOMAIN}_report.json"
  JIRA_OUT="${SANDBOX_REL}/${DOMAIN}_export_jira.csv"
  CONF_OUT="${SANDBOX_REL}/${DOMAIN}_export_confluence.html"
else
  REPORT="${SANDBOX_REL}/kfar_report.json"
  JIRA_OUT="${SANDBOX_REL}/kfar_export_jira.csv"
  CONF_OUT="${SANDBOX_REL}/kfar_export_confluence.html"
fi

REPORT_ABS="$(python -c "import os,sys; print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))" "$ROOT" "$REPORT")"
JIRA_ABS="$(python -c "import os,sys; print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))" "$ROOT" "$JIRA_OUT")"
CONF_ABS="$(python -c "import os,sys; print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))" "$ROOT" "$CONF_OUT")"

echo "==> [2/4] Ingesting SQL logs (discovery mode)..."
ama-ingest run \
  --data-root . \
  --sql-logs "${SANDBOX_REL}/sql_logs/*.jsonl" \
  --ddl-manifest "${DDL_MANIFEST}" \
  --glossary "${SANDBOX_REL}/glossary/*_glossary.json" \
  --glossary-dirty "${SANDBOX_REL}/glossary/*_glossary_dirty.json" \
  --comms-dir "${SANDBOX_REL}/comms" \
  --git-sql-roots "${SANDBOX_REL}/git_sql" \
  --target-schema dbo \
  --target-table orders \
  --discovery-mode --discovery-merge-all \
  --format json \
  -o "${REPORT}"

echo "==> [3/4] Export plan (Jira CSV import)..."
ama-ingest export-plan --report "${REPORT}" --format jira --out "${JIRA_OUT}"

echo "==> [4/4] Export plan (Confluence HTML)..."
ama-ingest export-plan --report "${REPORT}" --format confluence --out "${CONF_OUT}"

echo ""
echo "Demo outputs (under sandbox, absolute paths):"
echo "  Report JSON:          ${REPORT_ABS}"
echo "  Jira export:          ${JIRA_ABS}"
echo "  Confluence export:    ${CONF_ABS}"
echo "  Dashboard (copy/paste): ama-dashboard --report-path \"${REPORT_ABS}\""
