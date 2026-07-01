#!/usr/bin/env bash
# Подготовка VPS для деплоя через GitHub Actions (SSH-ключ deploy)
set -euo pipefail

HOST="${VPS_HOST:-195.133.73.215}"
USER="${VPS_USER:-root}"
KEY_PATH="${DEPLOY_KEY_PATH:-$HOME/.ssh/comm_proposals_deploy}"

if [[ ! -f "$KEY_PATH" ]]; then
  echo "Генерация ключа: $KEY_PATH"
  ssh-keygen -t ed25519 -C "github-actions-comm-proposals" -f "$KEY_PATH" -N ""
fi

echo ""
echo "=== 1. Добавьте публичный ключ на сервер ==="
echo "ssh ${USER}@${HOST} 'mkdir -p ~/.ssh && chmod 700 ~/.ssh'"
echo "ssh ${USER}@${HOST} \"echo '$(cat "${KEY_PATH}.pub")' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\""
echo ""
echo "=== 2. Секреты GitHub (Settings → Secrets → Actions) ==="
echo "VPS_HOST=${HOST}"
echo "VPS_USER=${USER}"
echo "VPS_SSH_KEY=<содержимое ${KEY_PATH}>"
echo "VPS_DOTENV=<содержимое production .env>"
echo ""
echo "=== 3. Проверка SSH ==="
echo "ssh -i ${KEY_PATH} ${USER}@${HOST} 'echo ok && mkdir -p /opt/comm-proposals'"
echo ""
echo "После push в main деплой запустится автоматически (workflow Deploy VPS)."
