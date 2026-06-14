#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="$(cd "$ROOT/.." && pwd)"
VENV="$ROOT/.venv"

echo "==> Meilisearch module setup"
echo "    folder:  $ROOT"
echo "    project: $PROJECT_ROOT"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
  echo "==> Created virtualenv: $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip
pip install -r "$ROOT/requirements.txt"

echo
echo "Done. Next steps:"
echo "  1. Add Meilisearch keys to $PROJECT_ROOT/.env (see meilisearch/env.example)"
echo "  2. Start server: docker compose up -d meilisearch"
echo "  3. Sync index:   $ROOT/scripts/sync_index.sh"
echo "  4. Test search:  $ROOT/scripts/search_cli.sh \"стол ученический\""
