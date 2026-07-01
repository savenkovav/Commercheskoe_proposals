#!/usr/bin/env bash
# Настройка зеркал Docker Hub (таймауты registry-1.docker.io на VPS)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON_JSON="/etc/docker/daemon.json"
SOURCE_JSON="${ROOT}/deploy/docker/daemon.json"

if [[ ! -f "$SOURCE_JSON" ]]; then
  echo "Не найден $SOURCE_JSON"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не установлен — пропуск настройки зеркал"
  exit 0
fi

mkdir -p /etc/docker

if [[ -f "$DAEMON_JSON" ]] && grep -q 'registry-mirrors' "$DAEMON_JSON" 2>/dev/null && grep -q '"dns"' "$DAEMON_JSON" 2>/dev/null; then
  echo "Зеркала и DNS Docker уже настроены: $DAEMON_JSON"
  exit 0
fi

if [[ -f "$DAEMON_JSON" ]]; then
  cp "$DAEMON_JSON" "${DAEMON_JSON}.bak.$(date +%Y%m%d%H%M%S)"
fi

cp "$SOURCE_JSON" "$DAEMON_JSON"
echo "Обновлён $DAEMON_JSON"

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart docker
  echo "Docker перезапущен"
fi
