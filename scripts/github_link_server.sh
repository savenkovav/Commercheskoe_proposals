#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/comm-proposals"
REMOTE="git@github.com:uchtender/Comm_proposals.git"

cd "$REPO_DIR"

echo "==> Проверка SSH к GitHub..."
if ! ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -qi "successfully authenticated"; then
  echo "Ошибка: GitHub не принимает SSH-ключ."
  echo "Добавьте публичный ключ в репозиторий:"
  echo "  Settings → Deploy keys → Add deploy key"
  echo "  Allow write access: включить (для push с сервера)"
  echo
  cat /root/.ssh/github_comm_proposals.pub
  exit 1
fi

echo "==> Получение данных с GitHub..."
git fetch origin || true

if git rev-parse --verify origin/main >/dev/null 2>&1; then
  echo "На GitHub уже есть ветка main."
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "Локальные коммиты есть — выполните вручную: git pull --rebase origin main"
  else
    echo "Подтягиваем origin/main..."
    git checkout -B main origin/main
  fi
else
  echo "Репозиторий на GitHub пустой или ветка main отсутствует."
  echo "После первого коммита выполните: git push -u origin main"
fi

echo "Готово. remote:"
git remote -v
