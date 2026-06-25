# Деплой на домен regionsnab7.ru (отложено)

> Сейчас используется **https://probizness.ru/** — см. [deploy/probizness.ru/README.md](../probizness.ru/README.md).

Публичный URL веб-интерфейса: **http://regionsnab7.ru/**

DNS: A-запись `regionsnab7.ru` и `www.regionsnab7.ru` → **195.133.73.215** (VPS с приложением).

Сейчас домен часто указывает на другой IP (`77.222.40.251`) — тогда сайт не откроется. Проверка:

```bash
dig +short regionsnab7.ru A
```

## Nginx (target-nginx на VPS)

Приложение работает в Docker (`comm-proposals-web:8080`), снаружи его отдаёт **target-nginx** (`/root/nginx.conf`).

```bash
bash scripts/configure_regionsnab7_nginx.sh
```

Фрагмент: `deploy/regionsnab7.ru/nginx-target-vhost.conf`

Альтернатива (профиль `beget-nginx` в docker-compose): `deploy/beget/nginx-docker.conf`

После изменения конфигурации:

```bash
bash scripts/configure_docker_mirrors.sh
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
```

Если сборка падает с `registry-1.docker.io ... i/o timeout`, на VPS уже используется зеркало
(`deploy/docker/daemon.json`) и базовый образ Python — `mirror.gcr.io/library/python:3.11-slim`.
