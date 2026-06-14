#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="$(cd "$ROOT/.." && pwd)"
VENV="$ROOT/.venv"
QUERY="${1:-}"

if [[ -z "$QUERY" ]]; then
  echo "Usage: $0 \"search query\"" >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  echo "Virtualenv not found. Run: $ROOT/scripts/setup.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
export PYTHONPATH="$PROJECT_ROOT:$ROOT"

python "$ROOT/scripts/search_cli.py" "$QUERY"
