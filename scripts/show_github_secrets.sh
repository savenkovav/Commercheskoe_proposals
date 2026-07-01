#!/usr/bin/env bash
# Показывает значения для GitHub Secrets (без вывода приватного ключа в консоль)
set -euo pipefail

KEY="$HOME/.ssh/comm_proposals_deploy"
DOTENV="${1:-$(cd "$(dirname "$0")/.." && pwd)/deploy/github-vps-dotenv.secret}"

echo "=== GitHub Secrets (Commercheskoe_proposals → Settings → Secrets) ==="
echo ""
echo "VPS_HOST=195.133.73.215"
echo "VPS_USER=root"
echo ""
echo "VPS_SSH_KEY → скопируйте файл:"
echo "  $KEY"
echo ""
if [[ -f "$DOTENV" ]]; then
  echo "VPS_DOTENV → скопируйте файл:"
  echo "  $DOTENV"
else
  echo "VPS_DOTENV → сначала: ./scripts/generate_vps_dotenv.sh"
fi
echo ""
echo "=== Публичный ключ (на сервер в ~/.ssh/authorized_keys) ==="
cat "${KEY}.pub"
echo ""
echo "Команда для сервера:"
echo "  ssh root@195.133.73.215 \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$(cat "${KEY}.pub")' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\""
