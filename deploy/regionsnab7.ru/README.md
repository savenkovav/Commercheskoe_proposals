# Деплой на домен regionsnab7.ru

Публичный URL веб-интерфейса: **http://regionsnab7.ru/**

DNS: A-запись `regionsnab7.ru` и `www.regionsnab7.ru` → IP VPS.

## Переменные окружения

```env
WEB_HOST=0.0.0.0
WEB_BEHIND_PROXY=true
PUBLIC_BASE_URL=http://regionsnab7.ru
```

## Nginx

- Docker (профиль `beget-nginx`): `deploy/beget/nginx-docker.conf`
- Внешний nginx на VPS: `deploy/regionsnab7.ru/nginx-vhost.conf`

После изменения конфигурации:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```
