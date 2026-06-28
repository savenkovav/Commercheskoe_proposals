#!/usr/bin/env bash
# Добавляет vhost regionsnab7.ru в /root/nginx.conf (target-nginx) и перезагружает nginx
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NGINX_CONF="/root/nginx.conf"
MARKER="regionsnab7.ru — КП-Ассистент"
SNIPPET="${ROOT}/deploy/regionsnab7.ru/nginx-target-vhost.conf"

if [[ ! -f "$SNIPPET" ]]; then
  echo "Не найден $SNIPPET"
  exit 1
fi

if grep -q "server_name regionsnab7.ru" "$NGINX_CONF" 2>/dev/null; then
  echo "Vhost regionsnab7.ru уже есть в $NGINX_CONF"
else
  cp "$NGINX_CONF" "${NGINX_CONF}.bak.$(date +%Y%m%d%H%M%S)"
  # Вставить перед закрывающей скобкой http { }
  export ROOT
  python3 - <<'PY'
from pathlib import Path
import os

root = Path(os.environ["ROOT"])
nginx = Path("/root/nginx.conf")
snippet = (root / "deploy/regionsnab7.ru/nginx-target-vhost.conf").read_text(encoding="utf-8")
text = nginx.read_text(encoding="utf-8")
if "server_name regionsnab7.ru" in text:
    raise SystemExit(0)
if not text.rstrip().endswith("}"):
    raise SystemExit("Unexpected nginx.conf format")
# last } closes http block
idx = text.rfind("}")
text = text[:idx] + "\n" + snippet + "\n" + text[idx:]
nginx.write_text(text, encoding="utf-8")
print(f"Добавлен vhost в {nginx}")
PY
fi

if docker ps --format '{{.Names}}' | grep -qx target-nginx; then
  docker exec target-nginx nginx -t
  docker exec target-nginx nginx -s reload
  echo "target-nginx перезагружен"
else
  echo "Контейнер target-nginx не найден — проверьте nginx вручную"
fi

echo ""
echo "Важно: DNS A-запись regionsnab7.ru должна указывать на IP этого VPS."
echo "Проверка: curl -I http://regionsnab7.ru/"
