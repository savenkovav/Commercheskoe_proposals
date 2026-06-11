# AI-агент коммерческих предложений (MVP)

Веб-приложение на Python для автоматического формирования коммерческих предложений (КП) в Excel с выставляемой наценкой.

## Возможности MVP

- Парсинг ТЗ заказчика из `.docx` (таблица с позициями)
- Поиск позиций в трёх источниках:
  1. **Каталог** компании (себестоимость)
  2. **Реестр остатков** (наличие на складе)
  3. **Прайсы поставщиков** (закупочные цены)
- AI-подбор через **ProxyAPI** (OpenAI-compatible) при неоднозначных совпадениях
- Оценка рыночной цены через AI, если позиция не найдена локально
- Расчёт себестоимости и наценки 30%
- Генерация Excel с листами:
  - **КП** — коммерческое предложение для заказчика (по образцу)
  - **Детализация** — статусы, источники, себестоимость, наценка, примечания
  - **Сводка** — статистика обработки

## Быстрый старт

### 1. Клонирование и настройка

```bash
git clone <your-repo-url> comm-proposals
cd comm-proposals
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

### 2. Заполните `.env`

| Переменная | Описание |
|---|---|
| `PROXYAPI_API_KEY` | Ключ с [proxyapi.ru](https://proxyapi.ru) |
| `MARKUP_PERCENT` | Наценка (по умолчанию 30) |

### 3. CLI-демо (локальный матчинг)

```bash
python scripts/run_demo.py --no-ai
```

С AI (нужен ключ ProxyAPI):

```bash
python scripts/run_demo.py
```

Результат сохраняется в `output/KP_*.xlsx`.

### 4. Запуск веб-интерфейса

```bash
./scripts/start_web.sh
```

Откройте http://127.0.0.1:8080 — загрузка ТЗ, формирование КП, управление прайсами и поиск позиций.

## Защита персональных данных (ПДн)

Перед каждым запросом к OpenAI через ProxyAPI данные проходят обезличивание:

- email, телефоны, URL, `@username`
- ИНН / КПП / ОГРН (с метками)
- СНИЛС, паспорт, банковские реквизиты
- адреса, ФИО (формат «Фамилия Имя Отчество»)
- реквизиты и адрес вашей компании из `.env`
- дополнительные термины из `PII_EXTRA_TERMS`

Настройки в `.env`:

```env
PII_ANONYMIZATION_ENABLED=true
PII_REDACT_ORG_DATA=true
PII_EXTRA_TERMS=Фамилия Иванов|Название заказчика
```

Проверка: `python scripts/test_pii_anonymizer.py`

## Деплой на облачный сервер

### Домен savenkoff.beget.tech (Beget VPS)

1. **Панель Beget → DNS** — A-запись `savenkoff.beget.tech` → IP вашего VPS.
2. На VPS установите Docker и откройте порты 80/443.
3. Скопируйте `.env` на сервер (`WEB_HOST=0.0.0.0`, `WEB_BEHIND_PROXY=true`).
4. Деплой через GitHub Actions (секреты `VPS_HOST`, `VPS_USER`, `VPS_PASSWORD`) или:

```bash
VPS_PASSWORD='...' ./scripts/vps_rsync_deploy.sh
```

На сервере поднимается `docker compose -f docker-compose.prod.yml` (приложение + nginx).

### Docker (локально)

```bash
cp env.example .env
nano .env
docker compose up -d --build
docker compose logs -f
```

### Systemd (без Docker)

```bash
# /etc/systemd/system/comm-proposals-web.service
[Unit]
Description=Comm Proposals Web
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/comm-proposals
Environment=PYTHONPATH=/opt/comm-proposals
ExecStart=/opt/comm-proposals/.venv/bin/python -m src.web.server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Структура проекта

```
├── data/                    # Каталог, прайсы, демо-ТЗ
├── output/                  # Сгенерированные КП
├── scripts/run_demo.py      # CLI-демо
├── src/
│   ├── config.py            # Конфигурация из .env
│   ├── web/server.py        # Веб-интерфейс (FastAPI)
│   └── services/
│       ├── data_loader.py   # Загрузка Excel/docx
│       ├── matcher.py       # Fuzzy-поиск (rapidfuzz)
│       ├── ai_agent.py      # ProxyAPI / OpenAI
│       ├── excel_generator.py
│       └── proposal_processor.py
├── Dockerfile
├── docker-compose.yml
└── env.example
```

## Логика подбора позиций

```
ТЗ (.docx)
    ↓
Fuzzy-поиск в каталоге → score ≥ 90 = EXACT
    ↓                    score 70-89 = SIMILAR
Реестр остатков
    ↓
Прайсы поставщиков
    ↓
AI (ProxyAPI) — семантический подбор + оценка цены
    ↓
Excel КП (наценка 30%)
```

## Данные MVP

В `data/` включены:
- `catalog.xlsx` — каталог (~148 позиций с себестоимостью)
- `registry.xlsx` — реестр остатков
- `price_prirodovedenie.xls` — прайс поставщика (~2156 позиций)
- `sample_tz.docx` — демо-ТЗ (14 позиций)

## Лицензия

Proprietary — для внутреннего использования.
