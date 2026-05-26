"""
publishers/x_publisher.py — Публикация в соцсеть X (Twitter).

Использует библиотеку tweepy для отправки твита (Client API v2).
Пропускает публикацию, если ключи не настроены в .env.
"""

import asyncio
from typing import Any
import tweepy

from config import cfg
from logger import log


def _format_x_post(row: Any) -> str:
    """
    Форматирует короткий пост для X (Twitter).
    Лимит твита — 280 символов.
    """
    short_text = row["ai_short"] or row["title"]
    url = row["url"]

    # Формируем текст
    post = f"{short_text}\n\n🔗 {url}"

    # Если превышает лимит X (280 символов), обрезаем
    if len(post) > 280:
        over_chars = len(post) - 280 + 3
        short_text = short_text[:-over_chars] + "..."
        post = f"{short_text}\n\n🔗 {url}"

    return post


def _publish_x_sync(text: str) -> bool:
    """Синхронная публикация в Twitter (вызывается в отдельном потоке)."""
    try:
        client = tweepy.Client(
            consumer_key=cfg.x_api_key,
            consumer_secret=cfg.x_api_secret,
            access_token=cfg.x_access_token,
            access_token_secret=cfg.x_access_token_secret,
            bearer_token=cfg.x_bearer_token if cfg.x_bearer_token else None,
        )

        client.create_tweet(text=text)
        return True
    except Exception as exc:
        log.error("❌ [X Publisher] Ошибка при создании твита: {}", exc)
        return False


async def publish_to_x(row: Any) -> bool:
    """
    Публикует новость в X (Twitter).
    
    Returns:
        True при успехе или если платформа не настроена (пропуск).
        False при ошибке публикации.
    """
    # Пропускаем, если X не настроен
    if (
        not cfg.x_api_key
        or not cfg.x_access_token
        or "your_api" in cfg.x_api_key
        or "your_access" in cfg.x_access_token
    ):
        log.info("⏭️  [X Publisher] Интеграция с X не настроена (пропуск)")
        return True

    # Для твита используем ai_short
    if not row["ai_short"]:
        log.error("❌ [X Publisher] ai_short пустой для новости #{}", row["id"])
        return False

    text = _format_x_post(row)
    log.info("📢 [X Publisher] Отправка твита для новости #{} в X...", row["id"])

    # Запускаем tweepy в пуле потоков
    success = await asyncio.to_thread(_publish_x_sync, text)
    if success:
        log.info("✅ [X Publisher] Твит для новости #{} успешно опубликован", row["id"])
    return success
