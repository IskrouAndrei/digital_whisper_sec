"""
publishers/habr_generator.py — Генератор еженедельного дайджеста для Хабра.

Каждое воскресенье (или по крону) бот собирает все опубликованные статьи за последние 7 дней,
генерирует Markdown-дайджест через LLM и отправляет его администратору в виде файла.
"""

import os
from datetime import datetime
from aiogram import Bot
from aiogram.types import FSInputFile

from database import Database
from llm_service import generate_weekly_digest
from logger import log
from config import cfg


async def generate_and_send_weekly_digest(db: Database, bot: Bot) -> bool:
    """
    Генерирует Markdown-дайджест для Хабра из новостей за последние 7 дней
    и прикрепляет его в чат администратора в виде файла.
    """
    log.info("⏰ [Habr Generator] Запуск генерации еженедельного дайджеста...")

    # 1. Извлекаем статьи, опубликованные за последние 7 дней
    news_rows = db.get_published_since(days=7)
    if not news_rows:
        log.warning("📭 [Habr Generator] Нет опубликованных новостей за последние 7 дней. Дайджест пуст.")
        try:
            await bot.send_message(
                chat_id=cfg.admin_chat_id,
                text="📭 <b>Еженедельный дайджест для Хабра:</b>\nЗа последние 7 дней не было опубликовано ни одной статьи. Дайджест не сгенерирован.",
                parse_mode="HTML"
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

    # 3. Сохраняем дайджест в файл в папке logs (пробрасывается хосту через Docker volume)
    os.makedirs("logs", exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"habr_digest_{date_str}.md"
    file_path = os.path.join("logs", filename)

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(digest_markdown)
        log.info("💾 [Habr Generator] Дайджест успешно сохранен в файл: {}", file_path)
    except Exception as exc:
        log.error("❌ [Habr Generator] Не удалось сохранить дайджест в файл: {}", exc)
        return False

    # 4. Отправляем файл дайджеста администратору в Telegram
    log.info("📨 [Habr Generator] Отправка файла дайджеста администратору в Telegram...")
    try:
        document = FSInputFile(file_path)
        await bot.send_document(
            chat_id=cfg.admin_chat_id,
            document=document,
            caption=(
                f"📝 <b>Еженедельный ИБ-дайджест для Хабра готов!</b>\n\n"
                f"📊 Всего статей за неделю: <b>{len(news_rows)}</b>\n"
                f"💾 Сохранен в: <code>{file_path}</code>\n\n"
                f"Вы можете скопировать содержимое файла и опубликовать на Хабре."
            ),
            parse_mode="HTML"
        )
        log.info("✅ [Habr Generator] Дайджест успешно отправлен администратору")
        return True
    except Exception as exc:
        log.error("❌ [Habr Generator] Не удалось отправить файл в Telegram: {}", exc)
        return False
