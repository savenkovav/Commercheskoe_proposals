#!/usr/bin/env bash
# Готовит содержимое для GitHub Secret VPS_DOTENV (production)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${1:-$ROOT/.env}"
OUT="${2:-$ROOT/deploy/github-vps-dotenv.secret}"

if [[ ! -f "$SOURCE" ]]; then
  echo "Не найден $SOURCE — скопируйте env.example в .env"
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

{
  echo "# Сгенерировано scripts/generate_vps_dotenv.sh — вставьте в GitHub Secret VPS_DOTENV"
  sed -e 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' \
      -e 's/^WEB_BEHIND_PROXY=.*/WEB_BEHIND_PROXY=true/' \
      -e 's|^PROCUREMENT_REPORT_PATH=.*|PROCUREMENT_REPORT_PATH=|' \
      -e 's/^USE_AI_INTERNET_SEARCH=.*/USE_AI_INTERNET_SEARCH=false/' \
      "$SOURCE"
  grep -q '^WEB_BEHIND_PROXY=' "$SOURCE" || echo 'WEB_BEHIND_PROXY=true'
  grep -q '^PUBLIC_BASE_URL=' "$SOURCE" || echo 'PUBLIC_BASE_URL=http://195.133.73.215'
  grep -q '^SEARCH_KIT_COMPONENT_LINKS=' "$SOURCE" || echo 'SEARCH_KIT_COMPONENT_LINKS=false'
} > "$OUT"

echo "Готово: $OUT"
echo "Скопируйте файл целиком в GitHub → Settings → Secrets → VPS_DOTENV"
