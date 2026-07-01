#!/usr/bin/env bash
# Деплой comm-proposals на VPS по SSH-ключу (Git Bash / WSL / Linux / macOS)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:-$ROOT/deploy/vps.deploy.env}"

if [[ ! -f "$CONFIG" ]]; then
  echo "Не найден $CONFIG"
  echo "Скопируйте deploy/vps.deploy.env.example в deploy/vps.deploy.env"
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG"

: "${VPS_HOST:?VPS_HOST не задан}"
: "${VPS_USER:?VPS_USER не задан}"
VPS_PORT="${VPS_PORT:-22}"
VPS_APP_DIR="${VPS_APP_DIR:-/opt/comm-proposals}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://${VPS_HOST}}"
DEPLOY_MODE="${DEPLOY_MODE:-standalone}"
EXPOSE_APP_PORT="${EXPOSE_APP_PORT:-true}"

if [[ -z "${VPS_SSH_KEY:-}" ]] || [[ ! -f "$VPS_SSH_KEY" ]]; then
  echo "SSH-ключ не найден: ${VPS_SSH_KEY:-}"
  exit 1
fi

SSH_OPTS=(-i "$VPS_SSH_KEY" -p "$VPS_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
REMOTE="${VPS_USER}@${VPS_HOST}"
ARCHIVE="$(mktemp /tmp/comm-proposals-release.XXXXXX.tgz)"
ENV_TMP="$(mktemp /tmp/comm-proposals-deploy.XXXXXX.env)"
trap 'rm -f "$ARCHIVE" "$ENV_TMP"' EXIT

chmod 600 "$VPS_SSH_KEY" 2>/dev/null || true

echo "==> VPS: ${REMOTE}"
echo "==> Каталог: ${VPS_APP_DIR}"
echo "==> URL: ${PUBLIC_BASE_URL}"

SOURCE_ENV="$ROOT/env.example"
[[ -f "$ROOT/.env" ]] && SOURCE_ENV="$ROOT/.env"

{
  sed -e 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' \
      -e 's/^WEB_BEHIND_PROXY=.*/WEB_BEHIND_PROXY=true/' \
      -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|" \
      -e 's|^PROCUREMENT_REPORT_PATH=.*|PROCUREMENT_REPORT_PATH=|' \
      -e 's/^MEILISEARCH_ENABLED=.*/MEILISEARCH_ENABLED=true/' \
      -e 's|^MEILISEARCH_HOST=.*|MEILISEARCH_HOST=http://meilisearch:7700|' \
      -e 's/^USE_AI_INTERNET_SEARCH=.*/USE_AI_INTERNET_SEARCH=false/' \
      "$SOURCE_ENV"
  grep -q '^WEB_BEHIND_PROXY=' "$SOURCE_ENV" || echo 'WEB_BEHIND_PROXY=true'
  grep -q '^AUTH_ENABLED=' "$SOURCE_ENV" || echo 'AUTH_ENABLED=true'
  grep -q '^USERS_DB_PATH=' "$SOURCE_ENV" || echo 'USERS_DB_PATH=data/users.db'
} > "$ENV_TMP"

echo "==> Сборка архива..."
tar czf "$ARCHIVE" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='kp_meilisearch/.venv' \
  --exclude='output' \
  --exclude='__pycache__' \
  --exclude='.env' \
  --exclude='.DS_Store' \
  --exclude='.cursor' \
  --exclude='deploy/vps.deploy.env' \
  --exclude='deploy/github-vps-dotenv.secret' \
  --exclude='id_rsa' \
  -C "$ROOT" .

ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p ${VPS_APP_DIR}"
scp "${SSH_OPTS[@]}" "$ARCHIVE" "${REMOTE}:/tmp/comm-proposals-release.tgz"
scp "${SSH_OPTS[@]}" "$ENV_TMP" "${REMOTE}:${VPS_APP_DIR}/.env"

ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '${VPS_APP_DIR}' && tar xzf /tmp/comm-proposals-release.tgz -C '${VPS_APP_DIR}' && rm -f /tmp/comm-proposals-release.tgz && find '${VPS_APP_DIR}' -name '*.sh' -exec sed -i 's/\\r$//' {} + && APP_DIR='${VPS_APP_DIR}' PUBLIC_BASE_URL='${PUBLIC_BASE_URL}' DEPLOY_MODE='${DEPLOY_MODE}' EXPOSE_APP_PORT='${EXPOSE_APP_PORT}' bash '${VPS_APP_DIR}/scripts/vps_remote_setup.sh'"

echo ""
echo "Готово: ${PUBLIC_BASE_URL}"
[[ "$EXPOSE_APP_PORT" == "true" ]] && echo "Прямой доступ: http://${VPS_HOST}:8080"

if grep -q 'PROXYAPI_API_KEY=your_proxyapi_key' "$ENV_TMP"; then
  echo ""
  echo "ВНИМАНИЕ: задайте PROXYAPI_API_KEY на сервере:"
  echo "  ssh -i \"${VPS_SSH_KEY}\" ${REMOTE} 'nano ${VPS_APP_DIR}/.env'"
fi
