# Деплой на домен probizness.ru

DNS: A-запись `probizness.ru` и `www.probizness.ru` → IP VPS.

На сервере с занятыми портами 80/443 приложение проксируется через основной nginx (`target-nginx`).

## Переменные `.env`

```env
WEB_HOST=0.0.0.0
WEB_BEHIND_PROXY=true
PUBLIC_BASE_URL=https://probizness.ru
```

## Запуск

```bash
docker compose up -d --build
```

Контейнер `comm-proposals-web` подключается к внешней сети `root_target_network`.

## Nginx

Конфигурация: `deploy/probizness.ru/nginx-vhost.conf`

## SSL

```bash
certbot certonly --webroot -w /var/www/certbot -d probizness.ru -d www.probizness.ru
```
