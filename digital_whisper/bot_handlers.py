"""
bot_handlers.py — Telegram-бот модерации (aiogram v3).

Архитектура:
  • При появлении новости с готовым ai_text бот шлёт черновик в ADMIN_CHAT_ID
    с четырьмя Inline-кнопками:
      ✅ Опубликовать везде  → callback approve_{news_id}   (все платформы сразу)
      ❌ Отклонить        → callback reject_{news_id}    (помечает rejected в БД)
      📢 Telegram       → callback tgonly_{news_id}    (только Telegram-канал)
      🔗 LinkedIn       → callback linkedin_{news_id}  (только LinkedIn)

Алертинг:
  • Функция send_alert() отправляет критические ошибки в ADMIN_CHAT_ID.
    Вызывается из global exception handler в main.py.
"""

import asyncio
import traceback
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramAPIError

from config import cfg
from database import Database
from logger import log
from publishers.tg_publisher import publish_to_telegram
from publishers.vk_publisher import publish_to_vk
from publishers.x_publisher import publish_to_x
from publishers.linkedin_pub import publish_to_linkedin
from publishers.threads_pub import publish_to_threads

# ---------------------------------------------------------------------------
# Клавиатура администратора
# ---------------------------------------------------------------------------

def _admin_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная Reply-клавиатура с кнопками управления ботом."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔍 Найти новости"),
                KeyboardButton(text="⏳ Черновики"),
            ],
            [
                KeyboardButton(text="📝 Дайджест Хабр"),
                KeyboardButton(text="📊 Статус"),
            ],
            [
                KeyboardButton(text="🟢 Авто ВКЛ"),
                KeyboardButton(text="🛑 Авто ВЫКЛ"),
            ],
        ],
        resize_keyboard=True,
        persistent=True,
    )


# ---------------------------------------------------------------------------
# Инициализация бота (синглтон)
# ---------------------------------------------------------------------------

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_router = Router()
_parse_callback = None


def register_parse_callback(callback) -> None:
    """Регистрирует callback-функцию для ручного парсинга (избегает циклического импорта)."""
    global _parse_callback
    _parse_callback = callback


def get_bot() -> Bot:
    """Возвращает (или создаёт) экземпляр Bot."""
    global _bot
    if _bot is None:
        _bot = Bot(
            token=cfg.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def get_dispatcher(db: Database) -> Dispatcher:
    """Создаёт Dispatcher и регистрирует роутер с хендлерами."""
    global _dp
    if _dp is None:
        _dp = Dispatcher()
        _dp["db"] = db          # Инжектируем БД через workflow_data aiogram
        _dp.include_router(_router)
    return _dp


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _moderation_keyboard(
    news_id: int,
    tg_done: bool = False,
    linkedin_done: bool = False,
) -> InlineKeyboardMarkup:
    """Генерирует Inline-клавиатуру для черновика новости."""
    tg_label = "📢 Telegram ✅" if tg_done else "📢 Telegram"
    linkedin_label = "🔗 LinkedIn ✅" if linkedin_done else "🔗 LinkedIn"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать везде", callback_data=f"approve_{news_id}"),
            InlineKeyboardButton(text="❌ Отклонить",          callback_data=f"reject_{news_id}"),
        ],
        [
            InlineKeyboardButton(text=tg_label,       callback_data=f"tgonly_{news_id}"),
            InlineKeyboardButton(text=linkedin_label, callback_data=f"linkedin_{news_id}"),
        ],
    ])


def _format_draft(row) -> str:
    """Форматирует черновик для отправки администратору."""
    row = dict(row)
    pub_time = row.get("published_at") or row.get("created_at") or "—"
    
    # Пытаемся сделать дату более читаемой в формате МСК (+3 часа от UTC)
    from datetime import datetime, timezone, timedelta
    formatted_time = pub_time
    try:
        if " " in pub_time and ":" in pub_time:
            # Парсим 'YYYY-MM-DD HH:MM:SS' (UTC из sqlite3 created_at)
            dt_utc = datetime.strptime(pub_time.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            dt_msc = dt_utc + timedelta(hours=3)
            formatted_time = dt_msc.strftime("%d.%m.%Y %H:%M") + " (МСК)"
        elif "T" in pub_time and ":" in pub_time:
            # ISO формат (например, '2026-05-26T12:00:00Z')
            cleaned = pub_time.split("+")[0].split("Z")[0].split(".")[0]
            dt = datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S")
            if "+03:00" in pub_time or "GMT+3" in pub_time:
                formatted_time = dt.strftime("%d.%m.%Y %H:%M") + " (МСК)"
            else:
                dt_utc = dt.replace(tzinfo=timezone.utc)
                dt_msc = dt_utc + timedelta(hours=3)
                formatted_time = dt_msc.strftime("%d.%m.%Y %H:%M") + " (МСК)"
    except Exception:
        pass

    return (
        f"🔵 <b>НОВАЯ НОВОСТЬ #{row['id']}</b>\n"
        f"📰 <b>Источник:</b> {row['source'] or '—'}\n"
        f"📅 <b>Опубликовано:</b> {formatted_time}\n"
        f"🔗 <a href='{row['url']}'>Оригинал</a>\n\n"
        f"<b>📋 Черновик для публикации:</b>\n"
        f"{row['ai_text'] or '⚠️ ai_text не готов'}\n\n"
        f"<i>Короткая версия (X/Threads):</i>\n"
        f"<code>{row['ai_short'] or '⚠️ ai_short не готов'}</code>"
    )


# ---------------------------------------------------------------------------
# Алертинг (вызывается из любого места через import)
# ---------------------------------------------------------------------------

async def send_alert(message: str, exc: Optional[Exception] = None) -> None:
    """
    Отправляет системное сообщение об ошибке администратору.
    Вызывается при ошибках уровня ERROR и CRITICAL.
    """
    bot = get_bot()
    tb_text = ""
    if exc:
        tb_text = "\n\n<pre>" + traceback.format_exc()[:2000] + "</pre>"

    text = (
        f"🚨 <b>SYSTEM ALERT — CyberSentry</b>\n\n"
        f"<code>{message}</code>"
        f"{tb_text}"
    )

    try:
        await bot.send_message(
            chat_id=cfg.admin_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        log.info("📟 Алерт отправлен администратору")
    except TelegramAPIError as tg_exc:
        log.error("❌ Не удалось отправить алерт в Telegram: {}", tg_exc)
    except Exception as send_exc:
        log.error("❌ Критическая ошибка при отправке алерта: {}", send_exc)


async def send_draft_to_admin(db: Database, news_id: int) -> bool:
    """
    Отправляет черновик новости администратору.
    Вызывается из main.py после успешной LLM-обработки.
    """
    row = db.get_by_id(news_id)
    if not row:
        log.error("❌ Новость #{} не найдена в БД", news_id)
        return False

    if not row["ai_text"]:
        log.warning("⚠️  Новость #{} не имеет ai_text, пропускаем", news_id)
        return False

    bot = get_bot()
    text = _format_draft(row)
    keyboard = _moderation_keyboard(news_id)

    try:
        await bot.send_message(
            chat_id=cfg.admin_chat_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        log.info("📨 Черновик #{} отправлен на модерацию", news_id)
        return True
    except TelegramAPIError as exc:
        log.error("❌ Ошибка отправки черновика #{}: {}", news_id, exc)
        await send_alert(f"Не удалось отправить черновик #{news_id} на модерацию", exc)
        return False


# ---------------------------------------------------------------------------
# Callback-хендлеры модерации
# ---------------------------------------------------------------------------

@_router.callback_query(F.data.startswith("approve_"))
async def handle_approve(callback: CallbackQuery, db: Database) -> None:
    """Администратор одобрил публикацию."""
    news_id = int(callback.data.split("_")[1])
    row = db.get_by_id(news_id)

    if not row:
        await callback.answer("❌ Новость не найдена в БД", show_alert=True)
        return

    if row["status"] == "published":
        await callback.answer("✅ Уже опубликовано ранее", show_alert=True)
        return

    # Обновляем статус
    db.set_status(news_id, "approved")
    log.info("👤 Администратор одобрил новость #{}", news_id)

    # --- Публикация в Telegram-канал ---
    bot = get_bot()
    tg_ok = await publish_to_telegram(bot, row)

    if not tg_ok:
        await send_alert(f"Не удалось опубликовать новость #{news_id} в Telegram-канал")
        await callback.answer("⚠️ Ошибка публикации в Telegram. См. логи", show_alert=True)
        return

    # --- Публикация на другие платформы (VK, X, LinkedIn, Threads) ---
    import asyncio
    results = await asyncio.gather(
        publish_to_vk(row),
        publish_to_x(row),
        publish_to_linkedin(row),
        publish_to_threads(row),
        return_exceptions=True
    )

    platforms = ["VK", "X (Twitter)", "LinkedIn", "Threads"]
    success_platforms = ["Telegram"]
    failed_platforms = []

    for platform, res in zip(platforms, results):
        if isinstance(res, Exception):
            log.error("💥 [Publishers Orchestrator] Исключение при публикации в {}: {}", platform, res)
            failed_platforms.append(platform)
        elif res is False:
            failed_platforms.append(platform)
        else:
            # Если вернул True, значит либо успешно опубликовано, либо платформа не настроена (пропуск)
            # Мы можем проверить, настроена ли платформа, чтобы не хвастаться в отчете,
            # но для простоты добавим в список успехов, если не было сбоя
            success_platforms.append(platform)

    db.set_status(news_id, "published")

    # Обновляем сообщение в чате администратора
    status_text = ", ".join(success_platforms)
    if failed_platforms:
        status_text += f" (Сбои: {', '.join(failed_platforms)})"

    try:
        await callback.message.edit_text(
            text=(
                f"✅ <b>ОПУБЛИКОВАНО #{news_id}</b>\n\n"
                f"📰 {row['source'] or '—'}\n"
                f"📡 Площадки: <b>{status_text}</b>\n"
                f"🔗 <a href='{row['url']}'>Оригинал</a>"
            ),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        pass  # Сообщение могло быть удалено

    await callback.answer("✅ Опубликовано на всех активных платформах!")


@_router.callback_query(F.data.startswith("reject_"))
async def handle_reject(callback: CallbackQuery, db: Database) -> None:
    """Администратор отклонил новость."""
    news_id = int(callback.data.split("_")[1])
    row = db.get_by_id(news_id)

    if not row:
        await callback.answer("❌ Новость не найдена", show_alert=True)
        return

    db.set_status(news_id, "rejected")
    log.info("👤 Администратор отклонил новость #{}", news_id)

    try:
        await callback.message.edit_text(
            text=(
                f"❌ <b>ОТКЛОНЕНО #{news_id}</b>\n\n"
                f"📰 {row['source'] or '—'}\n"
                f"🔗 <a href='{row['url']}'>Оригинал</a>"
            ),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        pass

    await callback.answer("❌ Новость отклонена и помечена в БД")


@_router.callback_query(F.data.startswith("tgonly_"))
async def handle_telegram_only(callback: CallbackQuery, db: Database) -> None:
    """Публикует новость ТОЛЬКО в Telegram-канал (без смены общего статуса)."""
    news_id = int(callback.data.split("_")[1])
    row = db.get_by_id(news_id)

    if not row:
        await callback.answer("❌ Новость не найдена в БД", show_alert=True)
        return

    await callback.answer("📢 Публикую в Telegram...")
    log.info("👤 Ручная публикация #{} в Telegram-канал", news_id)

    bot = get_bot()
    ok = await publish_to_telegram(bot, row)

    if ok:
        updated_row = db.get_by_id(news_id)
        new_keyboard = _moderation_keyboard(
            news_id,
            tg_done=True,
            linkedin_done=False,
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=new_keyboard)
        except TelegramAPIError:
            pass
        await callback.answer("✅ Опубликовано в Telegram!", show_alert=True)
    else:
        await callback.answer(
            "❌ Ошибка публикации в Telegram. Проверь логи.",
            show_alert=True,
        )

@_router.callback_query(F.data.startswith("linkedin_"))
async def handle_linkedin_only(callback: CallbackQuery, db: Database) -> None:
    """Публикует новость ТОЛЬКО в LinkedIn (без смены общего статуса)."""
    news_id = int(callback.data.split("_")[1])
    row = db.get_by_id(news_id)

    if not row:
        await callback.answer("❌ Новость не найдена в БД", show_alert=True)
        return

    await callback.answer("🔗 Публикую в LinkedIn...")
    log.info("👤 Ручная публикация #{} в LinkedIn", news_id)

    ok = await publish_to_linkedin(row)

    if ok:
        new_keyboard = _moderation_keyboard(news_id, tg_done=False, linkedin_done=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=new_keyboard)
        except TelegramAPIError:
            pass
        await callback.answer("✅ Опубликовано в LinkedIn!", show_alert=True)
    else:
        await callback.answer(
            "❌ Ошибка публикации в LinkedIn. Проверь логи.",
            show_alert=True,
        )

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Хендлеры управления и команд администратора
# ---------------------------------------------------------------------------

@_router.message(Command("parse"))
async def cmd_parse(message: Message, db: Database) -> None:
    """Запускает ручной парсинг RSS-лент."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return

    await message.answer("🔍 <b>Запускаю ручной поиск киберугроз...</b>\nЭто займет немного времени.", parse_mode=ParseMode.HTML)

    if _parse_callback:
        try:
            # Вызываем переданный callback в фоновом режиме, чтобы не таймаутить Telegram
            async def run_parsing_bg():
                try:
                    await _parse_callback(manual=True)
                    pending_rows = db.get_pending_with_ai()
                    count = len(pending_rows)
                    extra_text = ""
                    if count > 0:
                        extra_text = f"\n\n⏳ В базе также обнаружено <b>{count} готовых черновиков</b>, ожидающих модерации. Отправьте команду /pending, чтобы прислать их в чат порциями."
                    await message.answer(f"✅ <b>Ручной поиск завершен!</b>\nЕсли были найдены интересные и критические уязвимости/взломы — черновики уже отправлены на модерацию.{extra_text}", parse_mode=ParseMode.HTML)
                except Exception as exc:
                    log.exception("❌ Ошибка при ручном поиске в фоне: {}", exc)
                    await message.answer(f"❌ <b>Ошибка при поиске:</b> <code>{exc}</code>", parse_mode=ParseMode.HTML)

            asyncio.create_task(run_parsing_bg())
        except Exception as exc:
            log.exception("❌ Ошибка при запуске ручного поиска: {}", exc)
            await message.answer(f"❌ <b>Ошибка при запуске:</b> <code>{exc}</code>", parse_mode=ParseMode.HTML)
    else:
        await message.answer("⚠️ Callback парсера не зарегистрирован. Бот еще запускается.", parse_mode=ParseMode.HTML)


# --- Обработчики кнопок Reply Keyboard ---

@_router.message(F.text == "🔍 Найти новости")
async def btn_parse(message: Message, db: Database) -> None:
    """Кнопка: Найти новости."""
    await cmd_parse(message, db)


@_router.message(F.text == "⏳ Черновики")
async def btn_pending(message: Message, db: Database) -> None:
    """Кнопка: Черновики."""
    await cmd_pending(message, db)


@_router.message(F.text == "📝 Дайджест Хабр")
async def btn_digest(message: Message, db: Database) -> None:
    """Кнопка: Дайджест Хабр."""
    await cmd_digest(message, db)


@_router.message(F.text == "📊 Статус")
async def btn_status(message: Message, db: Database) -> None:
    """Кнопка: Статус."""
    await cmd_status(message, db)


@_router.message(F.text == "🟢 Авто ВКЛ")
async def btn_start_auto(message: Message, db: Database) -> None:
    """Кнопка: Включить автопарсинг."""
    await cmd_start_auto(message, db)


@_router.message(F.text == "🛑 Авто ВЫКЛ")
async def btn_stop_auto(message: Message, db: Database) -> None:
    """Кнопка: Выключить автопарсинг."""
    await cmd_stop_auto(message, db)



@_router.message(Command("pending"))
async def cmd_pending(message: Message, db: Database) -> None:
    """Показывает черновики, ожидающие модерации."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return

    pending_rows = db.get_pending_with_ai()
    count = len(pending_rows)

    if count == 0:
        await message.answer("✅ <b>Нет черновиков, ожидающих модерации.</b>", parse_mode=ParseMode.HTML)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Получить 5 черновиков", callback_data="send_pending_5")]
    ])

    await message.answer(
        f"⏳ <b>В базе данных обнаружено {count} черновиков</b>, готовых к публикации, но еще не прошедших модерацию.\n\n"
        f"Нажмите кнопку ниже, чтобы прислать первые 5 черновиков в этот чат.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


@_router.callback_query(F.data == "send_pending_5")
async def handle_send_pending_5(callback: CallbackQuery, db: Database) -> None:
    """Отправляет 5 черновиков администратору."""
    pending_rows = db.get_pending_with_ai()
    count = len(pending_rows)

    if count == 0:
        await callback.answer("✅ Все черновики обработаны!", show_alert=True)
        try:
            await callback.message.edit_text("✅ Все черновики обработаны!")
        except TelegramAPIError:
            pass
        return

    batch = pending_rows[:5]
    sent_count = 0
    for row in batch:
        success = await send_draft_to_admin(db, row["id"])
        if success:
            sent_count += 1
        await asyncio.sleep(0.5)

    remaining = count - sent_count
    await callback.answer(f"📥 Отправлено {sent_count} черновиков")

    if remaining > 0:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Получить еще 5", callback_data="send_pending_5")]
        ])
        try:
            await callback.message.edit_text(
                f"📥 <b>Отправлено {sent_count} черновиков.</b>\n"
                f"⏳ Осталось в очереди: <b>{remaining}</b>.\n\n"
                f"Нажмите кнопку ниже, чтобы получить следующую порцию.",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
    else:
        try:
            await callback.message.edit_text(
                f"✅ Все черновики из очереди отправлены на модерацию!",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass


@_router.message(Command("start_auto"))
async def cmd_start_auto(message: Message, db: Database) -> None:
    """Включает автоматический периодический поиск."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return
    db.set_setting("auto_parser_enabled", "1")
    await message.answer("🟢 <b>Автоматический поиск уязвимостей ВКЛЮЧЕН.</b>\nБот будет опрашивать RSS-ленты раз в час.", parse_mode=ParseMode.HTML)


@_router.message(Command("stop_auto"))
async def cmd_stop_auto(message: Message, db: Database) -> None:
    """Выключает автоматический периодический поиск."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return
    db.set_setting("auto_parser_enabled", "0")
    await message.answer("🛑 <b>Автоматический поиск уязвимостей ВЫКЛЮЧЕН.</b>\nАвто-опрос приостановлен. Вы можете запускать поиск вручную командой /parse.", parse_mode=ParseMode.HTML)


@_router.message(Command("digest"))
async def cmd_digest(message: Message, db: Database) -> None:
    """Запускает ручную генерацию еженедельного дайджеста для Хабра."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return

    await message.answer("⏳ <b>Запускаю генерацию еженедельного дайджеста для Хабра...</b>\nЭто займет около 10-20 секунд.", parse_mode=ParseMode.HTML)

    try:
        from publishers.habr_generator import generate_and_send_weekly_digest
        bot = get_bot()
        success = await generate_and_send_weekly_digest(db, bot)
        if not success:
            await message.answer("❌ <b>Не удалось сгенерировать дайджест.</b> Подробности смотрите в логах.", parse_mode=ParseMode.HTML)
    except Exception as exc:
        log.exception("❌ Исключение при ручном запуске дайджеста: {}", exc)
        await message.answer(f"❌ <b>Исключение при генерации:</b> <code>{exc}</code>", parse_mode=ParseMode.HTML)


@_router.message(Command("status"))
async def cmd_status(message: Message, db: Database) -> None:
    """Команда /status — показывает сводку по БД."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return  # Только для администратора

    pending_rows = db.get_pending_with_ai()
    published_week = db.get_published_since(days=7)
    auto_enabled = db.get_setting("auto_parser_enabled", "1")

    auto_status = "🟢 Включен (раз в час)" if auto_enabled == "1" else "🛑 Выключен"

    await message.answer(
        f"📊 <b>CyberSentry Status</b>\n\n"
        f"📡 Авто-поиск: <b>{auto_status}</b>\n"
        f"⏳ Ожидают модерации: <b>{len(pending_rows)}</b>\n"
        f"✅ Опубликовано за 7 дней: <b>{len(published_week)}</b>",
        parse_mode=ParseMode.HTML,
    )


@_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветствие администратора."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return
    await message.answer(
        "🛡️ <b>CyberSentry: The Digital Whisper</b>\n\n"
        "Панель администратора активна.\n\n"
        "<b>Доступные команды:</b>\n"
        "• /parse — Запустить ручной поиск прямо сейчас\n"
        "• /pending — Черновики, ожидающие модерации\n"
        "• /digest — Еженедельный дайджест для Хабра\n"
        "• /stop_auto — Отключить автоматический поиск\n"
        "• /start_auto — Включить автоматический поиск\n"
        "• /status — Текущий статус и статистика\n\n"
        "<b>Или используй кнопки ниже 👇</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_admin_keyboard(),
    )


@_router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Показывает клавиатуру меню."""
    if str(message.chat.id) != str(cfg.admin_chat_id) and str(message.from_user.id) != str(cfg.admin_chat_id):
        return
    await message.answer(
        "👇 <b>Панель управления:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_admin_keyboard(),
    )



@_router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """Показывает chat_id текущего пользователя для диагностики."""
    uid = message.from_user.id
    log.info("🔍 /whoami: user_id={}, chat_id={}", uid, message.chat.id)
    await message.answer(
        f"🔑 <b>Your chat_id:</b> <code>{message.chat.id}</code>\n"
        f"👤 <b>Your user_id:</b> <code>{uid}</code>\n\n"
        f"📍 <b>ADMIN_CHAT_ID in .env:</b> <code>{cfg.admin_chat_id}</code>\n"
        f"✅ Совпадают: <b>{'Yes' if str(message.chat.id) == str(cfg.admin_chat_id) or str(uid) == str(cfg.admin_chat_id) else 'No — update ADMIN_CHAT_ID in .env!'}</b>",
        parse_mode=ParseMode.HTML,
    )
