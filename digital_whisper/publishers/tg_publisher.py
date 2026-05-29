"""
publishers/tg_publisher.py — Публикация в Telegram-канал.

Форматирует и отправляет одобренную новость в TELEGRAM_CHANNEL_ID.
Возвращает message_id опубликованного поста для логирования.
"""

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from config import cfg
from logger import log


def _format_channel_post(row) -> str:
    """
    Форматирует финальный пост для Telegram-канала.
    Добавляет ссылку на оригинал и хэштеги кибербезопасности.
    """
    hashtags = "#кибербезопасность #infosec #cybersecurity"

    # Признак вирусного контента (для Пикабу)
    viral_badge = "\n\n🔥 <b>Trending</b>" if row["is_viral"] else ""

    # Поддержка переключения форматов
    row_dict = dict(row)
    selected_format = row_dict.get("selected_format") or "standard"
    if selected_format == "deep" and row_dict.get("ai_text_deep"):
        content_text = row_dict["ai_text_deep"]
    else:
        content_text = row_dict["ai_text"] or ""

    return (
        f"{content_text}"
        f"{viral_badge}\n\n"
        f"🔗 <a href='{row['url']}'>Источник: {row['source'] or 'Original'}</a>\n\n"
        f"{hashtags}"
    )


async def publish_to_telegram(bot: Bot, row) -> bool:
    """
    Публикует новость в Telegram-канал.

    Args:
        bot: Экземпляр aiogram Bot
        row: sqlite3.Row запись из таблицы news

    Returns:
        True при успехе, False при ошибке
    """
    row_dict = dict(row)
    selected_format = row_dict.get("selected_format") or "standard"
    if selected_format == "deep":
        if not row_dict.get("ai_text_deep"):
            log.error("❌ [TG Publisher] ai_text_deep пустой для новости #{}", row_dict["id"])
            return False
    else:
        if not row_dict.get("ai_text"):
            log.error("❌ [TG Publisher] ai_text пустой для новости #{}", row_dict["id"])
            return False

    text = _format_channel_post(row)

    # Telegram лимит — 4096 символов для HTML-сообщений
    if len(text) > 4000:
        text = text[:3997] + "..."

    try:
        msg = await bot.send_message(
            chat_id=cfg.telegram_channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,  # Превью ссылки для Telegram
        )
        log.info(
            "✅ [TG Publisher] Новость #{} опубликована в {} (msg_id={})",
            row["id"],
            cfg.telegram_channel_id,
            msg.message_id,
        )
        return True

    except TelegramAPIError as exc:
        log.error(
            "❌ [TG Publisher] Ошибка публикации новости #{}: {}",
            row["id"],
            exc,
        )
        return False
    except Exception as exc:
        log.exception(
            "💥 [TG Publisher] Неожиданная ошибка для новости #{}: {}",
            row["id"],
            exc,
        )
        return False
