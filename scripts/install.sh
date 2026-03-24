#!/usr/bin/env bash
set -euo pipefail

EXTRAS=()
PYTHON_BIN="${PYTHON_BIN:-}"

usage() {
  cat <<'EOF'
AMA installer (bash)

Usage:
  bash scripts/install.sh [--embed] [--viz] [--all] [--python <bin>] [--help]

Options:
  --embed         Install optional embedding dependencies
  --viz           Install optional visualization dependencies
  --all           Install both --embed and --viz extras
  --python <bin>  Python interpreter to use (default: python3, then python)
  --help          Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --embed)
      EXTRAS+=("embed")
      shift
      ;;
    --viz)
      EXTRAS+=("viz")
      shift
      ;;
    --all)
      EXTRAS+=("embed" "viz")
      shift
      ;;
    --python)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --python requires a value." >&2
        exit 1
      fi
      PYTHON_BIN="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "ERROR: Python not found. Install Python 3.11+ and retry." >&2
    exit 1
  fi
fi

echo "==> Using Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

echo "==> Creating virtual environment (.venv)"
"$PYTHON_BIN" -m venv .venv

if [[ -x ".venv/bin/python" ]]; then
  VENV_PY=".venv/bin/python"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
  VENV_PY=".venv/Scripts/python.exe"
else
  echo "ERROR: Could not locate Python inside .venv." >&2
  exit 1
fi

echo "==> Upgrading pip"
"$VENV_PY" -m pip install --upgrade pip

echo "==> Installing AMA (editable)"
"$VENV_PY" -m pip install -e .

if [[ ${#EXTRAS[@]} -gt 0 ]]; then
  UNIQUE_EXTRAS="$(printf "%s\n" "${EXTRAS[@]}" | awk '!seen[$0]++' | paste -sd, -)"
  echo "==> Installing extras: $UNIQUE_EXTRAS"
  "$VENV_PY" -m pip install -e ".[${UNIQUE_EXTRAS}]"
fi

echo
echo "Installation complete."
echo "Run commands with the venv Python to avoid path issues:"
echo "  $VENV_PY -m ama.cli --help"
echo "Or activate the environment:"
if [[ "$VENV_PY" == ".venv/bin/python" ]]; then
  echo "  source .venv/bin/activate"
else
  echo "  source .venv/Scripts/activate"
fi
