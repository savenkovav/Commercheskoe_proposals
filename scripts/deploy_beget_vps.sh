#!/usr/bin/env bash
# Деплой на Beget VPS: BEGET_SSH=user@IP ./scripts/deploy_beget_vps.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${BEGET_SSH:?Укажите BEGET_SSH=user@IP_вашего_VPS}"
REMOTE_DIR="${BEGET_REMOTE_DIR:-~/comm-proposals}"
USE_DOCKER="${BEGET_USE_DOCKER:-true}"

echo "→ Синхронизация в ${REMOTE}:${REMOTE_DIR}"

rsync -avz --delete \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude '.git/' \
  --exclude 'output/' \
  --exclude '__pycache__/' \
  --exclude '.env' \
  --exclude '.DS_Store' \
  "$ROOT/" "${REMOTE}:${REMOTE_DIR}/"

if ! ssh "$REMOTE" "test -f ${REMOTE_DIR}/.env"; then
  echo ""
  echo "На сервере нет .env — скопируйте и настройте:"
  echo "  scp env.example ${REMOTE}:${REMOTE_DIR}/.env"
  echo "  ssh ${REMOTE} 'nano ${REMOTE_DIR}/.env'"
  echo ""
  echo "Для продакшена в .env укажите:"
  echo "  WEB_HOST=0.0.0.0"
  echo "  WEB_BEHIND_PROXY=true"
  echo "  PROCUREMENT_REPORT_PATH=   (или путь к файлу на сервере)"
  exit 1
fi

if [[ "$USE_DOCKER" == "true" ]]; then
  ssh "$REMOTE" "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml up -d --build"
  echo ""
  echo "Готово. Проверьте: http://regionsnab7.ru/"
  echo "Логи: ssh ${REMOTE} 'cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml logs -f'"
else
  ssh "$REMOTE" "cd ${REMOTE_DIR} && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
  echo ""
  echo "Без Docker: установите systemd-сервис из deploy/beget/comm-proposals-web.service"
fi
