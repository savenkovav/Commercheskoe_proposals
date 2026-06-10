#!/usr/bin/env bash
# Первичная настройка VPS (запускается на сервере)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/comm-proposals}"
REPO_URL="${REPO_URL:-https://github.com/savenkovav/Comm_proposals.git}"

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

mkdir -p "$(dirname "$APP_DIR")"
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR"
  git fetch origin
  git reset --hard origin/main
fi

cd "$APP_DIR"
mkdir -p output data

if [[ ! -f .env ]]; then
  cp env.example .env
  sed -i 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' .env
  sed -i 's/^WEB_BEHIND_PROXY=.*/WEB_BEHIND_PROXY=true/' .env
  echo "Создан .env из env.example — заполните PROXYAPI_API_KEY и другие секреты"
fi

docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
