#!/usr/bin/env bash
# Настройка и перезапуск приложения на VPS (вызывается из GitHub Actions или вручную)
set -eu

APP_DIR="${APP_DIR:-/opt/comm-proposals}"
REPO_URL="${REPO_URL:-https://github.com/savenkovav/Commercheskoe_proposals.git}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://probizness.ru}"
DEPLOY_MODE="${DEPLOY_MODE:-proxy}"
EXPOSE_APP_PORT="${EXPOSE_APP_PORT:-false}"
DOCKER_NETWORK="${DOCKER_NETWORK:-root_target_network}"

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

if [[ -f scripts/configure_docker_mirrors.sh ]]; then
  bash scripts/configure_docker_mirrors.sh || true
fi

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
grep -q '^PUBLIC_BASE_URL=' .env || echo "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}" >> .env
sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|" .env
grep -q '^MEILISEARCH_ENABLED=' .env || echo 'MEILISEARCH_ENABLED=true' >> .env
sed -i 's/^MEILISEARCH_ENABLED=.*/MEILISEARCH_ENABLED=true/' .env
grep -q '^MEILISEARCH_HOST=' .env || echo 'MEILISEARCH_HOST=http://meilisearch:7700' >> .env
sed -i 's|^MEILISEARCH_HOST=.*|MEILISEARCH_HOST=http://meilisearch:7700|' .env

if grep -q '^PROCUREMENT_REPORT_PATH=\.\./' .env 2>/dev/null; then
  sed -i 's|^PROCUREMENT_REPORT_PATH=.*|PROCUREMENT_REPORT_PATH=|' .env
fi

if ! docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
  docker network create "$DOCKER_NETWORK"
  echo "Создана Docker-сеть: $DOCKER_NETWORK"
fi

COMPOSE_FILES=(-f docker-compose.prod.yml)
COMPOSE_PROFILES=()

if [[ "$DEPLOY_MODE" == "standalone" ]]; then
  COMPOSE_PROFILES+=(--profile beget-nginx)
  echo "Режим деплоя: standalone (nginx в Docker, порт 80)"
else
  echo "Режим деплоя: proxy (внешний nginx, сеть ${DOCKER_NETWORK})"
fi

if [[ "$EXPOSE_APP_PORT" == "true" ]]; then
  COMPOSE_FILES+=(-f deploy/docker-compose.standalone.yml)
  echo "Дополнительно открыт порт 8080"
fi

docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" up -d --build --remove-orphans
docker compose "${COMPOSE_FILES[@]}" ps
docker compose "${COMPOSE_FILES[@]}" logs --tail=30 kp-web || true
