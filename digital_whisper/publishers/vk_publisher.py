"""
publishers/vk_publisher.py — Публикация в сообщество ВКонтакте.

Использует vk_api для публикации на стену группы.
Пропускает публикацию с предупреждением в логах, если ключи не настроены.
"""

import asyncio
from typing import Any
import vk_api

from config import cfg
from logger import log


def _format_vk_post(row: Any) -> str:
    """Форматирует пост для стены ВКонтакте."""
    hashtags = "#кибербезопасность #infosec #cybersecurity"
    viral_badge = "\n\n🔥 Trending" if row["is_viral"] else ""

    row_dict = dict(row)
    selected_format = row_dict.get("selected_format") or "standard"
    if selected_format == "deep" and row_dict.get("ai_text_deep"):
        content_text = row_dict["ai_text_deep"]
    else:
        content_text = row_dict["ai_text"] or ""

    return (
        f"{content_text}"
        f"{viral_badge}\n\n"
        f"🔗 Источник: {row['source'] or 'Original'} ({row['url']})\n\n"
        f"{hashtags}"
    )


def _publish_vk_sync(text: str) -> bool:
    """Синхронная публикация через vk_api (вызывается в отдельном потоке)."""
    try:
        # Инициализируем сессию через API-токен группы или пользователя
        vk_session = vk_api.VkApi(token=cfg.vk_token)
        vk = vk_session.get_api()

        # Превращаем ID группы в отрицательное число для owner_id на стене
        # (в API VK стена группы задается как -group_id)
        group_id = int(cfg.vk_group_id.strip())
        owner_id = -abs(group_id)

        vk.wall.post(
            owner_id=owner_id,
            from_group=1,  # Публикация от имени сообщества
            message=text,
            attachments=None,
        )
        return True
    except Exception as exc:
        log.error("❌ [VK Publisher] Ошибка при wall.post во ВКонтакте: {}", exc)
        return False


async def publish_to_vk(row: Any) -> bool:
    """
    Публикует новость во ВКонтакте.
    
    Returns:
        True при успехе или если платформа не настроена (пропуск).
        False при ошибке публикации.
    """
    # Если интеграция не настроена, пропускаем шаг без ошибок
    if not cfg.vk_token or not cfg.vk_group_id or "your_vk" in cfg.vk_token:
        log.info("⏭️  [VK Publisher] Интеграция с VK не настроена (пропуск)")
        return True

    row_dict = dict(row)
    selected_format = row_dict.get("selected_format") or "standard"
    if selected_format == "deep":
        if not row_dict.get("ai_text_deep"):
            log.error("❌ [VK Publisher] ai_text_deep пустой для новости #{}", row_dict["id"])
            return False
    else:
        if not row_dict.get("ai_text"):
            log.error("❌ [VK Publisher] ai_text пустой для новости #{}", row_dict["id"])
            return False

    text = _format_vk_post(row)
    log.info("📢 [VK Publisher] Публикация новости #{} в VK...", row["id"])

    # Запускаем синхронную операцию в пуле потоков asyncio
    success = await asyncio.to_thread(_publish_vk_sync, text)
    if success:
        log.info("✅ [VK Publisher] Новость #{} успешно опубликована в VK", row["id"])
    return success
