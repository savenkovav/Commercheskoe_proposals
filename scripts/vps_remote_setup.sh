#!/usr/bin/env bash
# Настройка и перезапуск приложения на VPS (вызывается из GitHub Actions или вручную)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/comm-proposals}"
REPO_URL="${REPO_URL:-https://github.com/savenkovav/Commercheskoe_proposals.git}"

export DEBIAN_FRONTEND=noninteractive

if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-plugin || true
fi

mkdir -p "$APP_DIR"
cd "$APP_DIR"
mkdir -p output data

if [[ ! -f .env ]]; then
  cp env.example .env
  echo "Создан .env из env.example — задайте PROXYAPI_API_KEY и пути к data/"
fi

sed -i 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' .env
grep -q '^WEB_BEHIND_PROXY=' .env || echo 'WEB_BEHIND_PROXY=true' >> .env
sed -i 's/^WEB_BEHIND_PROXY=.*/WEB_BEHIND_PROXY=true/' .env
grep -q '^AUTH_ENABLED=' .env || echo 'AUTH_ENABLED=true' >> .env
grep -q '^USERS_DB_PATH=' .env || echo 'USERS_DB_PATH=data/users.db' >> .env

if grep -q '^PROCUREMENT_REPORT_PATH=\.\./' .env 2>/dev/null; then
  sed -i 's|^PROCUREMENT_REPORT_PATH=.*|PROCUREMENT_REPORT_PATH=|' .env
fi

docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=30 kp-web || true
