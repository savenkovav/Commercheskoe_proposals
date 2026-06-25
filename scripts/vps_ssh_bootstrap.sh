#!/usr/bin/env bash
# Первичный деплой на VPS (пароль только через VPS_PASSWORD в окружении)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${VPS_HOST:-195.133.73.215}"
USER="${VPS_USER:-root}"
APP_DIR="${VPS_APP_DIR:-/opt/comm-proposals}"

if [[ -z "${VPS_PASSWORD:-}" ]]; then
  echo "Укажите пароль: VPS_PASSWORD='...' $0"
  exit 1
fi

ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP"' EXIT

if [[ -f "$ROOT/.env" ]]; then
  sed -e 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' \
      -e 's/^WEB_BEHIND_PROXY=.*/WEB_BEHIND_PROXY=true/' \
      -e 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=http://regionsnab7.ru|' \
      -e 's|^PROCUREMENT_REPORT_PATH=.*|PROCUREMENT_REPORT_PATH=|' \
      "$ROOT/.env" > "$ENV_TMP"
else
  cp "$ROOT/env.example" "$ENV_TMP"
  sed -i '' 's/^WEB_HOST=.*/WEB_HOST=0.0.0.0/' "$ENV_TMP"
  echo 'WEB_BEHIND_PROXY=true' >> "$ENV_TMP"
  grep -q '^PUBLIC_BASE_URL=' "$ENV_TMP" || echo 'PUBLIC_BASE_URL=http://regionsnab7.ru' >> "$ENV_TMP"
fi

export VPS_PASSWORD HOST USER APP_DIR ENV_TMP ROOT

expect <<'EXPECT_EOF'
set timeout 600
set password $env(VPS_PASSWORD)
set host $env(HOST)
set user $env(USER)
set app_dir $env(APP_DIR)
set env_tmp $env(ENV_TMP)
set root $env(ROOT)

spawn ssh -o StrictHostKeyChecking=no ${user}@${host} "mkdir -p $app_dir && apt-get update -qq && apt-get install -y -qq git curl ca-certificates"
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}

spawn scp -o StrictHostKeyChecking=no $env_tmp ${user}@${host}:${app_dir}/.env
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}

spawn ssh -o StrictHostKeyChecking=no ${user}@${host} "export APP_DIR=$app_dir REPO_URL=https://github.com/savenkovav/Commercheskoe_proposals.git; if \[ ! -f $app_dir/scripts/vps_remote_setup.sh \]; then apt-get update -qq && apt-get install -y -qq git curl ca-certificates && git clone \$REPO_URL \$APP_DIR; fi; bash $app_dir/scripts/vps_remote_setup.sh"
expect {
  -re "(?i)password:" { send "$password\r"; exp_continue }
  eof
}
EXPECT_EOF

echo ""
echo "Проверка: http://${HOST}/"
