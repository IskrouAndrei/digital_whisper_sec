# CyberSentry: The Digital Whisper

## Шаг 1 — Инфраструктура, Docker, Логи, БД, Парсер

### Быстрый старт (локально, без Docker)

```bash
cd digital_whisper

# 1. Создай виртуальное окружение
python3.11 -m venv .venv && source .venv/bin/activate

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Заполни переменные окружения
cp .env.example .env
# Отредактируй .env — минимум нужны: TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, TELEGRAM_CHANNEL_ID, OPENAI_API_KEY

# 4. Запусти
python main.py
```

### Запуск через Docker Compose

```bash
cd digital_whisper
cp .env.example .env
# Отредактируй .env

docker compose up --build -d
docker compose logs -f
```

---

## Структура проекта (Шаг 1)

```
digital_whisper/
├── .env.example          ✅  Шаблон переменных окружения
├── .env                  ⚙️  Твои реальные ключи (НЕ коммитить в git!)
├── Dockerfile            ✅  Python 3.11-slim образ
├── docker-compose.yml    ✅  volumes для /data и /logs
├── requirements.txt      ✅  Все зависимости с пинами версий
├── config.py             ✅  Загрузка и валидация .env
├── logger.py             ✅  loguru: консоль + ротирующий файл
├── database.py           ✅  SQLite: таблица news + CRUD
├── parser.py             ✅  Async RSS-парсер (6 источников)
├── main.py               ✅  APScheduler + точка входа
├── logs/                 ✅  Папка логов (Volume в Docker)
└── publishers/
    └── __init__.py       ✅  Пакет (будет заполнен в Шагах 3-5)
```

---

## Что будет дальше (Шаг 2)

- `llm_service.py` — интеграция OpenAI gpt-4o-mini для генерации `ai_text` и `ai_short`
- `bot_handlers.py` — aiogram v3 бот с кнопками ✅ Опубликовать / ❌ Отклонить / 🔥 На Пикабу
- Telegram Alerting — уведомления в `ADMIN_CHAT_ID` при ошибках уровня ERROR/CRITICAL
