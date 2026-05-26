"""
publishers/threads_pub.py — Публикация в Meta Threads.

Использует Meta Threads Graph API (двухшаговый процесс публикации).
Пропускает публикацию, если токены не настроены.
"""

import aiohttp
from typing import Any

from config import cfg
from logger import log


def _format_threads_text(row: Any) -> str:
    """
    Форматирует короткий пост для Threads.
    Threads поддерживает до 500 символов в одном текстовом посте.
    Мы используем ai_short или обрезанный ai_text.
    """
    text = row["ai_short"] or row["title"]
    url = row["url"]

    # Лимит Threads — 500 символов.
    post = f"{text}\n\n🔗 {url}"
    if len(post) > 500:
        over_chars = len(post) - 500 + 3
        text = text[:-over_chars] + "..."
        post = f"{text}\n\n🔗 {url}"

    return post


async def publish_to_threads(row: Any) -> bool:
    """
    Публикует новость в Meta Threads.
    Проходит двухшаговый флоу Graph API:
      1. Создание контейнера публикации (POST /threads)
      2. Публикация контейнера (POST /threads_publish)
      
    Returns:
        True при успехе или если платформа не настроена (пропуск).
        False при ошибке публикации.
    """
    # Пропускаем, если Threads не настроен
    if (
        not cfg.threads_access_token
        or not cfg.threads_user_id
        or "your_threads" in cfg.threads_access_token
    ):
        log.info("⏭️  [Threads Publisher] Интеграция с Threads не настроена (пропуск)")
        return True

    if not row["ai_short"]:
        log.error("❌ [Threads Publisher] ai_short пустой для новости #{}", row["id"])
        return False

    text = _format_threads_text(row)
    user_id = cfg.threads_user_id.strip()
    log.info("📢 [Threads Publisher] Публикация новости #{} в Threads...", row["id"])

    # Шаг 1: Создание медиа-контейнера
    container_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    container_params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": cfg.threads_access_token
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Создаем контейнер
            async with session.post(container_url, params=container_params, timeout=15.0) as response:
                if response.status != 200:
                    err_text = await response.text()
                    log.error(
                        "❌ [Threads Publisher] Не удалось создать контейнер. Статус: {}, Ответ: {}",
                        response.status,
                        err_text
                    )
                    return False
                
                container_data = await response.json()
                container_id = container_data.get("id")
                
                if not container_id:
                    log.error("❌ [Threads Publisher] В ответе API нет container_id: {}", container_data)
                    return False

            log.debug("📦 [Threads Publisher] Контейнер создан (id={})", container_id)

            # Шаг 2: Публикуем контейнер
            publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
            publish_params = {
                "creation_id": container_id,
                "access_token": cfg.threads_access_token
            }

            async with session.post(publish_url, params=publish_params, timeout=15.0) as response:
                if response.status != 200:
                    err_text = await response.text()
                    log.error(
                        "❌ [Threads Publisher] Ошибка при публикации контейнера. Статус: {}, Ответ: {}",
                        response.status,
                        err_text
                    )
                    return False
                
                publish_data = await response.json()
                post_id = publish_data.get("id")
                log.info("✅ [Threads Publisher] Пост #{} успешно опубликован в Threads (post_id={})", row["id"], post_id)
                return True

    except Exception as exc:
        log.exception("💥 [Threads Publisher] Неожиданная ошибка Threads API: {}", exc)
        return False
