"""
publishers/habr_generator.py — Генератор еженедельного дайджеста для Хабра.

Каждое воскресенье (или по крону) бот собирает все опубликованные статьи за последние 7 дней,
генерирует Markdown-дайджест через LLM и отправляет текст прямо в Telegram сообщениями.
"""

import os
from datetime import datetime
from aiogram import Bot

from database import Database
from llm_service import generate_weekly_digest
from logger import log
from config import cfg


TG_MAX_LEN = 4000  # Оставляем запас до 4096


def _split_text(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Разбивает длинный текст на части по целым строкам, не разрывая параграфы."""
    if len(text) <= max_len:
        return [text]

    parts = []
    current = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 за \n
        if current_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        parts.append("\n".join(current))

    return parts


async def generate_and_send_weekly_digest(db: Database, bot: Bot) -> bool:
    """
    Генерирует текстовый дайджест для Хабра из новостей за последние 7 дней
    и отправляет его прямо в Telegram-чат администратора частями.
    """
    log.info("⏰ [Habr Generator] Запуск генерации еженедельного дайджеста...")

    # 1. Извлекаем статьи, опубликованные за последние 7 дней
    news_rows = db.get_published_since(days=7)
    if not news_rows:
        log.warning("💭 [Habr Generator] Нет опубликованных новостей за последние 7 дней.")
        try:
            await bot.send_message(
                chat_id=cfg.admin_chat_id,
                text="💭 <b>Еженедельный дайджест для Хабра:</b>\nЗа последние 7 дней не было опубликовано ни одной статьи. Дайджест не сгенерирован.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.error("❌ Не удалось отправить сообщение о пустом дайджесте: {}", exc)
        return True

    # 2. Вызываем LLM-генератор
    log.info("🤖 [Habr Generator] Передаем {} статей в LLM для синтеза дайджеста...", len(news_rows))
    digest_markdown = await generate_weekly_digest(news_rows)

    if not digest_markdown:
        log.error("❌ [Habr Generator] LLM вернул пустой дайджест")
        return False

    # 3. Сохраняем резервную копию в файл
    os.makedirs("logs", exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"habr_digest_{date_str}.md"
    file_path = os.path.join("logs", filename)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(digest_markdown)
        log.info("💾 [Habr Generator] Резервная копия дайджеста: {}", file_path)
    except Exception as exc:
        log.warning("⚠️ [Habr Generator] Не удалось сохранить файл: {}", exc)

    # 4. Отправляем текст прямо в Telegram (разбивая по частям)
    log.info("📨 [Habr Generator] Отправка дайджеста администратору...")
    try:
        # Заголовок
        await bot.send_message(
            chat_id=cfg.admin_chat_id,
            text=(
                f"📝 <b>Еженедельный ИБ-дайджест для Хабра — {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
                f"📊 Статей за неделю: <b>{len(news_rows)}</b>\n"
                f"↓ Копируйте текст из сообщений ниже — он готов к публикации на Хабре."
            ),
            parse_mode="HTML",
        )

        # Отправляем частями как обычный текст (Markdown спецсимволы не ломаются)
        parts = _split_text(digest_markdown)
        for i, part in enumerate(parts, 1):
            prefix = f"📖 Часть {i}/{len(parts)}:\n\n" if len(parts) > 1 else ""
            await bot.send_message(
                chat_id=cfg.admin_chat_id,
                text=prefix + part,
                disable_web_page_preview=True,
            )

        log.info("✅ [Habr Generator] Дайджест отправлен администратору ({} частей)", len(parts))
        return True
    except Exception as exc:
        log.error("❌ [Habr Generator] Не удалось отправить дайджест в Telegram: {}", exc)
        return False
