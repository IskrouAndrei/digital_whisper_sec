"""
bot_handlers.py — Telegram-бот модерации (aiogram v3).

Архитектура:
  • При появлении новости с готовым ai_text бот шлёт черновик в ADMIN_CHAT_ID
    с тремя Inline-кнопками:
      ✅ Опубликовать   → callback approve_{news_id}
      ❌ Отклонить      → callback reject_{news_id}
      🔥 На Пикабу      → callback viral_{news_id}  (помечает is_viral=1, потом Approve)

  • При нажатии ✅ — вызывает все паблишеры (Шаги 3-4), обновляет статус → published
  • При нажатии ❌ — обновляет статус → rejected
  • При нажатии 🔥 — устанавливает is_viral=1, затем ждёт ✅ или ❌

Алертинг:
  • Функция send_alert() отправляет критические ошибки в ADMIN_CHAT_ID.
    Вызывается из global exception handler в main.py.
"""

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
)
from aiogram.exceptions import TelegramAPIError

from config import cfg
from database import Database
from logger import log
from publishers.tg_publisher import publish_to_telegram

# ---------------------------------------------------------------------------
# Инициализация бота (синглтон)
# ---------------------------------------------------------------------------

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_router = Router()


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

def _moderation_keyboard(news_id: int, is_viral: bool = False) -> InlineKeyboardMarkup:
    """Генерирует Inline-клавиатуру для черновика новости."""
    viral_label = "🔥 Пикабу (отмечено)" if is_viral else "🔥 На Пикабу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать",  callback_data=f"approve_{news_id}"),
            InlineKeyboardButton(text="❌ Отклонить",     callback_data=f"reject_{news_id}"),
        ],
        [
            InlineKeyboardButton(text=viral_label,        callback_data=f"viral_{news_id}"),
        ],
    ])


def _format_draft(row) -> str:
    """Форматирует черновик для отправки администратору."""
    status_icon = "🔥" if row["is_viral"] else "🔵"
    return (
        f"{status_icon} <b>НОВАЯ НОВОСТЬ #{row['id']}</b>\n"
        f"📰 <b>Источник:</b> {row['source'] or '—'}\n"
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
    keyboard = _moderation_keyboard(news_id, is_viral=bool(row["is_viral"]))

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

    # TODO Шаг 3-4: vk_publisher, x_publisher, linkedin_pub, threads_pub

    db.set_status(news_id, "published")

    # Обновляем сообщение в чате администратора
    try:
        await callback.message.edit_text(
            text=(
                f"✅ <b>ОПУБЛИКОВАНО #{news_id}</b>\n\n"
                f"📰 {row['source'] or '—'} | "
                f"🔥 Пикабу: {'Да' if row['is_viral'] else 'Нет'}\n"
                f"🔗 <a href='{row['url']}'>Оригинал</a>"
            ),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        pass  # Сообщение могло быть удалено

    await callback.answer("✅ Опубликовано на всех платформах!")


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


@_router.callback_query(F.data.startswith("viral_"))
async def handle_viral(callback: CallbackQuery, db: Database) -> None:
    """Администратор пометил новость как вирусную (для Пикабу)."""
    news_id = int(callback.data.split("_")[1])
    row = db.get_by_id(news_id)

    if not row:
        await callback.answer("❌ Новость не найдена", show_alert=True)
        return

    # Тоглим флаг
    new_viral = not bool(row["is_viral"])
    db.set_viral(news_id, new_viral)
    log.info("🔥 Вирусный флаг #{} → {}", news_id, new_viral)

    # Обновляем клавиатуру с новым состоянием кнопки
    # Перечитываем из БД для актуального состояния
    updated_row = db.get_by_id(news_id)
    new_keyboard = _moderation_keyboard(news_id, is_viral=bool(updated_row["is_viral"]))

    try:
        await callback.message.edit_reply_markup(reply_markup=new_keyboard)
    except TelegramAPIError:
        pass

    status = "отмечена 🔥" if new_viral else "флаг снят"
    await callback.answer(f"Новость #{news_id} {status}")


# ---------------------------------------------------------------------------
# Хендлер команды /status (диагностика)
# ---------------------------------------------------------------------------

@_router.message(Command("status"))
async def cmd_status(message: Message, db: Database) -> None:
    """Команда /status — показывает сводку по БД."""
    if str(message.from_user.id) != str(cfg.admin_chat_id):
        return  # Только для администратора

    pending_rows = db.get_pending_with_ai()
    published_week = db.get_published_since(days=7)

    await message.answer(
        f"📊 <b>CyberSentry Status</b>\n\n"
        f"⏳ Ожидают модерации: <b>{len(pending_rows)}</b>\n"
        f"✅ Опубликовано за 7 дней: <b>{len(published_week)}</b>",
        parse_mode=ParseMode.HTML,
    )


@_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветствие администратора."""
    if str(message.from_user.id) != str(cfg.admin_chat_id):
        return
    await message.answer(
        "🛡️ <b>CyberSentry: The Digital Whisper</b>\n\n"
        "Бот активен. Новости появятся автоматически по мере парсинга RSS-лент.\n\n"
        "Команды:\n"
        "• /status — статистика системы",
        parse_mode=ParseMode.HTML,
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
        f"✅ Совпадают: <b>{'Yes' if str(uid) == str(cfg.admin_chat_id) else 'No — update ADMIN_CHAT_ID in .env!'}</b>",
        parse_mode=ParseMode.HTML,
    )
