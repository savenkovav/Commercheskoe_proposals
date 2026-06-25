#!/usr/bin/env bash
# Синхронизация проекта на VPS (для приватного репозитория)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${VPS_HOST:-195.133.73.215}"
USER="${VPS_USER:-root}"
APP_DIR="${VPS_APP_DIR:-/opt/comm-proposals}"

if [[ -z "${VPS_PASSWORD:-}" ]]; then
  echo "Укажите пароль: VPS_PASSWORD='...' $0"
  exit 1
fi

export VPS_PASSWORD HOST USER APP_DIR ROOT

expect <<'EXPECT_EOF'
set timeout 900
set password $env(VPS_PASSWORD)
set host $env(HOST)
set user $env(USER)
set app_dir $env(APP_DIR)
set root $env(ROOT)

spawn ssh -o StrictHostKeyChecking=no ${user}@${host} "mkdir -p $app_dir"
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}

spawn bash -c "cd $root && tar czf - --exclude=.git --exclude=.venv --exclude=venv --exclude=output --exclude=__pycache__ --exclude=.DS_Store . | ssh -o StrictHostKeyChecking=no ${user}@${host} 'tar xzf - -C $app_dir'"
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}

spawn ssh -o StrictHostKeyChecking=no ${user}@${host} "export APP_DIR=$app_dir; bash $app_dir/scripts/vps_remote_setup.sh"
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}
EXPECT_EOF

echo "Готово: http://regionsnab7.ru/"
