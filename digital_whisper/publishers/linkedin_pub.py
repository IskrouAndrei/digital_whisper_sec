"""
publishers/linkedin_pub.py — Публикация в LinkedIn через REST API (ugcPosts).

Использует aiohttp для асинхронных HTTP-запросов к API LinkedIn.
Пропускает публикацию, если токены не настроены.
"""

import aiohttp
from typing import Any

from config import cfg
from logger import log


def _format_linkedin_text(row: Any) -> str:
    """Форматирует текст сообщения для LinkedIn."""
    hashtags = "#cybersecurity #infosec #cybersec"
    viral_badge = "\n\n🔥 Trending" if row["is_viral"] else ""
    
    return (
        f"{row['ai_text']}"
        f"{viral_badge}\n\n"
        f"🔗 Original feed: {row['url']}\n\n"
        f"{hashtags}"
    )


async def publish_to_linkedin(row: Any) -> bool:
    """
    Публикует новость в LinkedIn с оформлением в виде Link Card.
    
    Returns:
        True при успехе или если платформа не настроена (пропуск).
        False при ошибке публикации.
    """
    # Пропускаем, если LinkedIn не настроен
    if (
        not cfg.linkedin_access_token
        or not cfg.linkedin_person_urn
        or "your_linkedin" in cfg.linkedin_access_token
    ):
        log.info("⏭️  [LinkedIn Publisher] Интеграция с LinkedIn не настроена (пропуск)")
        return True

    if not row["ai_text"]:
        log.error("❌ [LinkedIn Publisher] ai_text пустой для новости #{}", row["id"])
        return False

    text = _format_linkedin_text(row)
    urn = cfg.linkedin_person_urn.strip()

    # Если URN не содержит префикса, добавляем по умолчанию urn:li:person:
    if not urn.startswith("urn:li:"):
        urn = f"urn:li:person:{urn}"

    log.info("📢 [LinkedIn Publisher] Публикация новости #{} в LinkedIn...", row["id"])

    # Формируем JSON-тело согласно LinkedIn ugcPosts API
    payload = {
        "author": urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {
                    "text": text
                },
                "shareMediaCategory": "ARTICLE",
                "media": [
                    {
                        "status": "READY",
                        "originalUrl": row["url"],
                        "title": {
                            "text": row["title"]
                        }
                    }
                ]
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }

    headers = {
        "Authorization": f"Bearer {cfg.linkedin_access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.linkedin.com/v2/ugcPosts",
                json=payload,
                headers=headers,
                timeout=15.0
            ) as response:
                if response.status in (200, 201):
                    log.info("✅ [LinkedIn Publisher] Новость #{} успешно опубликована в LinkedIn", row["id"])
                    return True
                
                error_body = await response.text()
                log.error(
                    "❌ [LinkedIn Publisher] Ошибка API LinkedIn. Статус: {}, Ответ: {}",
                    response.status,
                    error_body
                )
                return False

    except Exception as exc:
        log.exception("💥 [LinkedIn Publisher] Неожиданная ошибка при запросе к LinkedIn API: {}", exc)
        return False
