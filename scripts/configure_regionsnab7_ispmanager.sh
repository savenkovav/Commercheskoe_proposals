#!/usr/bin/env bash
# Прокси regionsnab7.ru → comm-proposals (ISPmanager nginx на VPS)
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/deploy/regionsnab7.ru/nginx-ispmanager-vhost.conf"
DEST="/etc/nginx/vhosts/www-root/regionsnab7.ru.conf"

if [[ ! -f "$SRC" ]]; then
  echo "Не найден $SRC"
  exit 1
fi

cp "$DEST" "${DEST}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
cp "$SRC" "$DEST"
nginx -t
systemctl reload nginx
echo "Nginx vhost обновлён: $DEST"
echo "Проверка: curl -I -H 'Host: regionsnab7.ru' http://127.0.0.1/"
