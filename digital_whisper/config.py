"""
config.py — Централизованная загрузка и валидация переменных окружения.
Использует python-dotenv + dataclass для строгой типизации.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Получить обязательную переменную окружения или бросить ошибку."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"[Config] Обязательная переменная окружения '{key}' не задана в .env!"
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _optional_int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    # Убираем inline-комментарии вида "15    # some comment"
    val = val.split("#")[0].strip()
    try:
        return int(val)
    except ValueError:
        raise EnvironmentError(
            f"[Config] Переменная '{key}' должна быть целым числом, получено: '{val}'"
        )


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    telegram_bot_token: str
    admin_chat_id: str
    telegram_channel_id: str

    # --- LLM (DeepSeek / OpenAI-compatible) ---
    llm_api_key: str
    llm_base_url: str
    llm_model: str

    # --- VK ---
    vk_token: str
    vk_group_id: str

    # --- X (Twitter) ---
    x_api_key: str
    x_api_secret: str
    x_access_token: str
    x_access_token_secret: str
    x_bearer_token: str

    # --- LinkedIn ---
    linkedin_access_token: str
    linkedin_person_urn: str
    linkedin_user_id: str         # Алиас: LINKEDIN_USER_ID (приоритет над person_urn)

    # --- Threads ---
    threads_user_id: str
    threads_access_token: str

    # --- RSS ---
    rss_feeds: list[str]

    # --- Scheduler ---
    parser_interval_minutes: int
    digest_day_of_week: str
    digest_hour: int

    # --- Database ---
    database_path: str

    # --- Logging ---
    log_level: str
    log_file_path: str


def load_config() -> Config:
    """Загрузить и вернуть валидированный объект конфигурации."""

    # --- LLM: DeepSeek (primary) или OpenAI (fallback) ---
    llm_api_key = (
        _optional("DEEPSEEK_API_KEY")
        or _optional("OPENAI_API_KEY")
    )
    if not llm_api_key:
        raise EnvironmentError(
            "[Config] Нужен хотя бы один ключ: DEEPSEEK_API_KEY или OPENAI_API_KEY"
        )

    llm_base_url = _optional("DEEPSEEK_BASE_URL", "https://api.openai.com/v1")
    llm_model = (
        _optional("DEEPSEEK_MODEL")
        or _optional("OPENAI_MODEL")
        or "deepseek-chat"
    )

    raw_feeds = _optional("RSS_FEEDS", (
        "https://feeds.feedburner.com/TheHackersNews,"
        "https://www.bleepingcomputer.com/feed/,"
        "https://securelist.com/feed/,"
        "https://unit42.paloaltonetworks.com/feed/,"
        "https://www.darkreading.com/rss.xml,"
        "https://krebsonsecurity.com/feed/"
    ))
    feeds = [f.strip() for f in raw_feeds.split(",") if f.strip()]

    return Config(
        # Telegram
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        admin_chat_id=_optional("ADMIN_CHAT_ID", "").split("#")[0].strip(),
        telegram_channel_id=_optional("TELEGRAM_CHANNEL_ID", "").split("#")[0].strip(),

        # LLM
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,

        # VK (опционально — заполнять при интеграции)
        vk_token=_optional("VK_TOKEN"),
        vk_group_id=_optional("VK_GROUP_ID"),

        # X (опционально)
        x_api_key=_optional("X_API_KEY"),
        x_api_secret=_optional("X_API_SECRET"),
        x_access_token=_optional("X_ACCESS_TOKEN"),
        x_access_token_secret=_optional("X_ACCESS_TOKEN_SECRET"),
        x_bearer_token=_optional("X_BEARER_TOKEN"),

        # LinkedIn (опционально)
        linkedin_access_token=_optional("LINKEDIN_ACCESS_TOKEN"),
        linkedin_person_urn=_optional("LINKEDIN_PERSON_URN"),
        linkedin_user_id=_optional("LINKEDIN_USER_ID"),

        # Threads (опционально)
        threads_user_id=_optional("THREADS_USER_ID"),
        threads_access_token=_optional("THREADS_ACCESS_TOKEN"),

        # RSS
        rss_feeds=feeds,

        # Scheduler
        parser_interval_minutes=_optional_int("PARSER_INTERVAL_MINUTES", 60),
        digest_day_of_week=_optional("DIGEST_DAY_OF_WEEK", "sun"),
        digest_hour=_optional_int("DIGEST_HOUR", 9),

        # Database
        database_path=_optional("DATABASE_PATH", "digital_whisper.db"),

        # Logging
        log_level=_optional("LOG_LEVEL", "INFO"),
        log_file_path=_optional("LOG_FILE_PATH", "logs/bot.log"),
    )


# Синглтон — импортируй везде как `from config import cfg`
cfg: Config = load_config()
