# Meilisearch для КП-Ассистента

Быстрый полнотекстовый поиск по **каталогу**, **реестру остатков** и **прайсам** с опечатками и ранжированием «из коробки».

## Структура

```
kp_meilisearch/
  .venv/                 # изолированное окружение (scripts/setup.sh)
  kp_search/             # Python-пакет: индексация и поиск
  scripts/
    setup.sh             # создать venv и установить зависимости
    sync_index.sh        # переиндексация из data/*.xlsx
    search_cli.sh        # быстрый тест поиска из терминала
  requirements.txt
  env.example
```

## Быстрый старт

### 1. Установка окружения модуля

```bash
./kp_meilisearch/scripts/setup.sh
```

### 2. Настройка `.env` в корне проекта

```env
MEILISEARCH_ENABLED=true
MEILISEARCH_HOST=http://127.0.0.1:7700
MEILISEARCH_API_KEY=masterKey
MEILISEARCH_INDEX=products
MEILISEARCH_AUTO_SYNC=true
MEILISEARCH_SEARCH_LIMIT=20
```

### 3. Запуск Meilisearch

```bash
docker compose up -d meilisearch
```

Проверка: http://127.0.0.1:7700/health

### 4. Индексация данных

```bash
./kp_meilisearch/scripts/sync_index.sh
```

### 5. Тест поиска

```bash
./kp_meilisearch/scripts/search_cli.sh "стол ученический"
```

## Интеграция с приложением

- При `MEILISEARCH_ENABLED=true` `ItemMatcher` сначала берёт кандидатов из Meilisearch, затем дополняет rapidfuzz.
- При загрузке/обновлении каталога, реестра и прайсов (`MEILISEARCH_AUTO_SYNC=true`) индекс обновляется автоматически.
- Статус в API: `GET /api/status` → поле `meilisearch`.

## Docker

В `docker-compose.yml` сервис `meilisearch` слушает порт **7700**.  
Контейнер `kp-web` получает `MEILISEARCH_HOST=http://meilisearch:7700`.

## Зависимости

- Изолированный venv: `kp_meilisearch/requirements.txt`
- Основное приложение: `meilisearch` добавлен в корневой `requirements.txt`
