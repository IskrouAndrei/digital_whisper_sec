"""
main.py — Точка входа CyberSentry: The Digital Whisper.

Запускает параллельно:
  1. APScheduler — периодический парсинг RSS каждые N минут
  2. aiogram Bot — polling для модерации и алертинга

Конвейер обработки новости:
  RSS → parser.py → database.py (pending) →
  llm_service.py (ai_text + ai_short) →
  bot_handlers.py (черновик → ADMIN_CHAT_ID) →
  [Approve] → publishers/* → status=published
"""

import asyncio
import sys
import traceback

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot_handlers import get_bot, get_dispatcher, send_alert, send_draft_to_admin
from config import cfg
from database import Database
from llm_service import generate_post
from logger import log, setup_logger
from parser import parse_and_store


# ---------------------------------------------------------------------------
# Глобальный exception handler
# ---------------------------------------------------------------------------

def _setup_global_exc_handler(bot: Bot) -> None:
    """
    Перехватывает необработанные исключения в asyncio event loop.
    При ERROR/CRITICAL — отправляет алерт администратору через Telegram.
    """
    loop = asyncio.get_event_loop()

    def handle_exception(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "Неизвестная ошибка event loop")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else msg

        log.error("💥 Необработанное исключение в event loop:\n{}", tb)

        # Отправляем Telegram-алерт (не ждём результата из sync-контекста)
        asyncio.ensure_future(
            send_alert(f"UNCAUGHT EXCEPTION:\n{msg}\n\n{tb[:1500]}", exc)
        )

    loop.set_exception_handler(handle_exception)
    log.info("🛡️  Глобальный exception handler зарегистрирован")


# ---------------------------------------------------------------------------
# Пайплайн обработки новостей
# ---------------------------------------------------------------------------

async def process_new_articles(db: Database, new_ids: list[int]) -> None:
    """
    Для каждой новой статьи:
      1. Генерирует ai_text и ai_short через OpenAI
      2. Сохраняет в БД
      3. Отправляет черновик на модерацию в Telegram
    """
    if not new_ids:
        return

    log.info("🔄 Запуск LLM-обработки {} новых статей...", len(new_ids))

    for news_id in new_ids:
        row = db.get_by_id(news_id)
        if not row:
            continue

        try:
            ai_text, ai_short = await generate_post(
                title=row["title"],
                raw_text=row["raw_text"] or "",
                url=row["url"],
                source=row["source"] or "Unknown",
            )

            if ai_text == "SKIP":
                db.set_status(news_id, "rejected")
                log.info("⏭️  Новость #{} отфильтрована ИИ как неинтересная (SKIP)", news_id)
                continue

            if ai_text:
                db.update_ai_content(news_id, ai_text, ai_short or "")
                # Отправляем черновик администратору
                await send_draft_to_admin(db, news_id)
            else:
                log.warning(
                    "⚠️  LLM не вернул ai_text для новости #{} — пропускаем",
                    news_id,
                )

        except Exception as exc:
            log.exception("❌ Ошибка LLM-обработки новости #{}: {}", news_id, exc)
            await send_alert(
                f"Ошибка LLM-обработки новости #{news_id}: {exc}",
                exc,
            )

        # Небольшая пауза между запросами к OpenAI (rate limit)
        await asyncio.sleep(2)


async def _process_pending_queue(db: Database) -> None:
    """
    Запускается при старте в фоне — обрабатывает pending-статьи без ai_text.
    Обрабатывает не более 5 статей за раз, чтобы не спамить.
    Не блокирует polling бота.
    """
    await asyncio.sleep(3)  # Даём polling подняться
    pending_ids = db.get_pending_ids(limit=5)
    if not pending_ids:
        log.info("✅ Очередь pending пустая, LLM не нужен")
        return
    total_pending = db.get_pending_ids(limit=10000)  # Узнаём реальный размер очереди
    log.info("📋 Запуск LLM-обработки {}/{} pending-статей в фоне...", len(pending_ids), len(total_pending))
    await process_new_articles(db, pending_ids)


# ---------------------------------------------------------------------------
# Задание планировщика
# ---------------------------------------------------------------------------

async def parser_job(db: Database, manual: bool = False) -> None:
    """
    Задание APScheduler:
      1. Проверяет, включен ли автоматический парсинг (если не ручной запуск)
      2. Парсит RSS-ленты → добавляет новые статьи
      3. Обрабатывает все pending-статьи из БД (включая ранее сброшенные)
    """
    if not manual:
        enabled = db.get_setting("auto_parser_enabled", "1")
        if enabled == "0":
            log.info("💤 [Scheduler] Автоматический парсинг отключен администратором — пропускаем")
            return

    try:
        log.info("⏰ [Scheduler] Запуск RSS-парсинга...")
        new_ids = await parse_and_store(db)

        if new_ids:
            log.info("📥 Найдено {} новых статей из RSS", len(new_ids))

        # Обрабатываем ВСЕ pending-статьи (новые + ранее накопленные без ai_text)
        all_pending_ids = db.get_pending_ids()
        if all_pending_ids:
            log.info("🔄 Запуск LLM-обработки {} pending-статей...", len(all_pending_ids))
            await process_new_articles(db, all_pending_ids)
        else:
            log.info("💤 [Scheduler] Нет pending-статей для обработки")

    except Exception as exc:
        log.exception("💥 Критическая ошибка в parser_job: {}", exc)
        await send_alert(
            f"[CRITICAL] Ошибка RSS-парсера:\n{exc}\n\n{traceback.format_exc()[:1000]}",
            exc,
        )


async def weekly_digest_job(db: Database, bot: Bot) -> None:
    """Задание планировщика для генерации еженедельного дайджеста."""
    try:
        from publishers.habr_generator import generate_and_send_weekly_digest
        await generate_and_send_weekly_digest(db, bot)
    except Exception as exc:
        log.exception("💥 Ошибка в weekly_digest_job: {}", exc)
        await send_alert(f"[CRITICAL] Ошибка еженедельного дайджеста:\n{exc}", exc)


async def token_expiry_check_job(db: Database, bot) -> None:
    """
    Ежедневная проверка срока жизни токенов OAuth (LinkedIn и др.).
    LinkedIn токены живут 60 дней. Отправляет напоминание за 2 дня до истечения.
    """
    from datetime import datetime, timezone, timedelta
    from aiogram.enums import ParseMode

    # Токен LinkedIn настроен?
    access_token = (cfg.linkedin_access_token or "").split("#")[0].strip()
    if not access_token or access_token.startswith("your_"):
        return  # LinkedIn не настроен — проверять нечего

    today = datetime.now(timezone.utc).date()

    # При первом запуске — сохраняем дату выдачи токена
    issue_date_str = db.get_setting("linkedin_token_issued_at", "")
    if not issue_date_str:
        db.set_setting("linkedin_token_issued_at", today.isoformat())
        log.info("📅 LinkedIn токен: дата выдачи зафиксирована — {}", today.isoformat())
        return

    try:
        issue_date = datetime.fromisoformat(issue_date_str).date()
    except ValueError:
        log.warning("⚠️ Не удалось разобрать дату токена LinkedIn: {}", issue_date_str)
        return

    linkedin_token_lifetime_days = 60
    expiry_date = issue_date + timedelta(days=linkedin_token_lifetime_days)
    days_left = (expiry_date - today).days

    log.info("🔑 LinkedIn токен: выдан {}, истекает {}, осталось {} дн.",
             issue_date_str, expiry_date.isoformat(), days_left)

    # Напоминание за 2 дня и за 1 день
    if days_left <= 2:
        urgency = "🆘 СРОЧНО" if days_left <= 1 else "⚠️ ВНИМАНИЕ"
        try:
            await bot.send_message(
                chat_id=cfg.admin_chat_id,
                text=(
                    f"{urgency} <b>LinkedIn токен истекает через {days_left} дн.!</b>\n\n"
                    f"📅 Дата выдачи: <code>{issue_date_str}</code>\n"
                    f"📅 Дата истечения: <code>{expiry_date.isoformat()}</code>\n\n"
                    "<b>Что сделать:</b>\n"
                    "1. Зайди на <a href='https://www.linkedin.com/developers/apps'>LinkedIn Developers</a>\n"
                    "2. Обнови Access Token в разделе OAuth\n"
                    "3. Замени <code>LINKEDIN_ACCESS_TOKEN</code> в <code>.env</code> на сервере:\n"
                    "<code>nano /root/digital_whisper_sec/digital_whisper/.env</code>\n"
                    "4. После обновления токена выполни:\n"
                    "<code>docker compose restart digital_whisper</code>\n\n"
                    "После рестарта таймер сбросится автоматически."
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            log.warning("⚠️ Отправлено напоминание об истечении LinkedIn токена ({} дн. осталось)", days_left)
        except Exception as exc:
            log.error("❌ Не удалось отправить напоминание о токене: {}", exc)

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    # 1. Логгер
    setup_logger(
        log_file_path=cfg.log_file_path,
        log_level=cfg.log_level,
    )
    log.info("🚀 CyberSentry: The Digital Whisper запускается...")
    log.info("   Модель LLM: {}", cfg.llm_model)
    log.info("   API: {}", cfg.llm_base_url)
    log.info("   Интервал парсинга: {} мин.", cfg.parser_interval_minutes)
    log.info("   БД: {}", cfg.database_path)

    # 2. БД
    db = Database(cfg.database_path)

    # 3. Bot + Dispatcher
    bot = get_bot()
    dp = get_dispatcher(db)

    # 4. Глобальный exception handler
    _setup_global_exc_handler(bot)

    # 5. Планировщик
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Периодический парсинг
    scheduler.add_job(
        parser_job,
        trigger="interval",
        minutes=cfg.parser_interval_minutes,
        args=[db],
        id="rss_parser",
        name="RSS Parser",
        replace_existing=True,
        max_instances=1,
    )

    # Еженедельный дайджест для Хабра
    scheduler.add_job(
        weekly_digest_job,
        trigger="cron",
        day_of_week=cfg.digest_day_of_week,
        hour=cfg.digest_hour,
        minute=0,
        args=[db, bot],
        id="weekly_digest",
        name="Weekly Habr Digest",
        replace_existing=True,
    )

    # Ежедневная проверка срока действия OAuth-токенов (LinkedIn и др.)
    scheduler.add_job(
        token_expiry_check_job,
        trigger="cron",
        hour=10,      # 10:00 UTC = 13:00 MSK
        minute=0,
        args=[db, bot],
        id="token_expiry_check",
        name="Token Expiry Check",
        replace_existing=True,
    )

    # Регистрируем callback для ручного парсинга в Telegram-боте
    # Оборачиваем в замыкание, чтобы db передавалась автоматически
    from bot_handlers import register_parse_callback

    async def _parser_job_with_db(manual: bool = False) -> None:
        await parser_job(db, manual=manual)

    register_parse_callback(_parser_job_with_db)

    scheduler.start()
    log.info("⏰ Планировщик запущен")

    # 6. Обработка pending-очереди в фоне (asyncio.create_task)
    asyncio.create_task(_process_pending_queue(db))

    # 7. Уведомляем администратора о старте
    try:
        await bot.send_message(
            chat_id=cfg.admin_chat_id,
            text=(
                "🟢 <b>CyberSentry запущен!</b>\n\n"
                f"🤖 Модель: <code>{cfg.llm_model}</code>\n"
                f"⏱ Интервал парсинга: <b>{cfg.parser_interval_minutes} мин.</b>\n"
                f"📡 RSS-источников: <b>{len(cfg.rss_feeds)}</b>\n\n"
                "Бот готов к модерации. /status — статистика."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("⚠️  Не удалось отправить стартовое сообщение: {}", exc)

    # 7. Запускаем polling (блокирующий)
    log.info("🤖 Запуск Telegram polling...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
        )
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("🛑 Завершение работы...")
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("👋 CyberSentry остановлен")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
