# AI-агент коммерческих предложений (MVP)

Telegram-бот на Python для автоматического формирования коммерческих предложений (КП) в Excel с наценкой 30%.

## Возможности MVP

- Парсинг ТЗ заказчика из `.docx` (таблица с позициями)
- Поиск позиций в трёх источниках:
  1. **Каталог** компании (себестоимость)
  2. **Реестр остатков** (наличие на складе)
  3. **Прайсы поставщиков** (закупочные цены)
- AI-подбор через **ProxyAPI** (OpenAI-compatible) при неоднозначных совпадениях
- Оценка рыночной цены через AI, если позиция не найдена локально
- Расчёт себестоимости и наценки 30%
- Генерация Excel с 3 листами:
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
| `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather) |
| `PROXYAPI_API_KEY` | Ключ с [proxyapi.ru](https://proxyapi.ru) |
| `MARKUP_PERCENT` | Наценка (по умолчанию 30) |

### 3. Демо без Telegram (локальный матчинг)

```bash
python scripts/run_demo.py --no-ai
```

С AI (нужен ключ ProxyAPI):

```bash
python scripts/run_demo.py
```

Результат сохраняется в `output/KP_*.xlsx`.

### 4. Запуск Telegram-бота

```bash
python -m src.main
```

Команды бота:
- `/start` — приветствие
- `/demo` — обработать демо-ТЗ из `data/sample_tz.docx`
- `/status` — статистика загруженных данных
- Отправка `.docx` — обработка вашего ТЗ

### Админ-команды (управление прайсами)

В `.env` укажите свой Telegram ID в `ADMIN_USER_IDS` (узнать ID: [@userinfobot](https://t.me/userinfobot)).

| Команда | Описание |
|---|---|
| `/admin` | Справка по админ-командам |
| `/prices` | Список загруженных прайсов |
| `/price_add Название\|Поставщик` | Добавить прайс → отправить `.xls`/`.xlsx` |
| `/price_replace id` | Заменить файл прайса → отправить новый файл |
| `/price_rename id Название\|Поставщик` | Изменить название/поставщика |
| `/price_remove id` | Удалить прайс |
| `/cancel` | Отменить ожидание загрузки |

Прайсы хранятся в `data/prices/`, реестр — `data/prices_registry.json`. После добавления/замены поиск обновляется автоматически.

## Защита персональных данных (ПДн)

Перед каждым запросом к OpenAI через ProxyAPI данные проходят обезличивание:

- email, телефоны, URL, Telegram `@username`
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

## Поиск позиции по названию

Команда `/find` или текстовый запрос на естественном языке:

```
/find термометр лабораторный | цена, остаток
/find мольберт | себестоимость, количество
сколько стоит палочка стеклянная?
```

Доступные поля: `цена`, `себестоимость`, `остаток`, `количество`, `единица`, `код`, `поставщик`.
Если поля не указаны — выводятся себестоимость, цена КП, остаток и ед. изм.

## Деплой на облачный сервер

### Docker (рекомендуется)

```bash
# На сервере
git clone <your-repo-url> /opt/comm-proposals
cd /opt/comm-proposals
cp env.example .env
nano .env  # заполните ключи

docker compose up -d --build
docker compose logs -f
```

### Systemd (без Docker)

```bash
# /etc/systemd/system/kp-bot.service
[Unit]
Description=KP Telegram Bot
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/comm-proposals
Environment=PYTHONPATH=/opt/comm-proposals
ExecStart=/opt/comm-proposals/.venv/bin/python -m src.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable kp-bot
sudo systemctl start kp-bot
```

## Структура проекта

```
├── data/                    # Каталог, прайсы, демо-ТЗ
├── output/                  # Сгенерированные КП
├── scripts/run_demo.py      # CLI-демо
├── src/
│   ├── main.py              # Точка входа бота
│   ├── config.py            # Конфигурация из .env
│   ├── bot/handlers.py      # Telegram handlers
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

## Масштабирование

Для production-версии планируется:
- PostgreSQL / Elasticsearch для каталога на тысячи позиций
- Несколько прайсов через `PRICE_LISTS` (comma-separated)
- Веб-поиск реальных цен (API маркетплейсов)
- Админ-панель для управления каталогом

## Лицензия

Proprietary — для внутреннего использования.
