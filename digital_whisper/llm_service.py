"""
llm_service.py — Взаимодействие с LLM API (DeepSeek / OpenAI-совместимый).

DeepSeek полностью совместим с OpenAI Python SDK через параметр base_url.
Генерирует два формата контента из сырого RSS-текста:
  • ai_text   — полноформатный пост для Telegram/VK (до 950 символов)
               с сохранением технической терминологии (CVE, RCE, APT и т.д.)
  • ai_short  — сверхкраткая версия для X/Threads (до 240 символов)

Отдельный метод для еженедельного Markdown-дайджеста (Шаг 5).
"""

import asyncio
from typing import Optional

from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError

from config import cfg
from logger import log

# ---------------------------------------------------------------------------
# Системные промпты
# ---------------------------------------------------------------------------

_SYSTEM_CYBERSEC = """Ты — старший аналитик информационной безопасности и технический редактор.
Твоя задача — адаптировать новости кибербезопасности для русскоязычного ИБ-сообщества.

ПРАВИЛА (строго обязательны):
1. Сохраняй ВСЮ техническую терминологию на английском: CVE-XXXX-XXXXX, RCE, LPE, APT, IoC, TTPs, CVSS, PoC, 0-day.
2. Пиши на русском языке, кроме терминов из п.1.
3. Используй структуру: краткое резюме → техническая суть → что делать / на что обратить внимание.
4. Добавляй релевантные эмодзи в начало каждого блока (🔴 критично, 🟡 важно, 🔵 информация).
5. ЗАПРЕЩЕНО: упрощать технические детали, убирать CVE-номера, делать "попсовый" пересказ.
6. Максимальная длина ai_text: 950 символов."""

_SYSTEM_SHORT = """Ты — копирайтер для X (Twitter).
Задача: сжать техническую новость кибербезопасности до 240 символов.

ПРАВИЛА:
1. Сохраняй CVE-номера, аббревиатуры (RCE, APT и т.д.) — они НЕ сокращаются.
2. Пиши на русском. Добавь 1-2 тематических хэштега в конце (#cybersecurity #infosec).
3. Максимум: 240 символов включая хэштеги.
4. Не используй переносы строк."""

_SYSTEM_DIGEST = """Ты — технический редактор раздела "Информационная безопасность" на Хабре.
Задача: составить еженедельный дайджест из списка новостей.

ПРАВИЛА:
1. Группируй новости по категориям: Уязвимости и патчи | APT и кампании | Утечки данных | Инструменты и исследования.
2. Для каждой новости: заголовок → 2-3 предложения технического описания → ссылка.
3. Сохраняй CVE-номера, CVSS-оценки, технические аббревиатуры.
4. Формат: Markdown, пригодный для публикации на Хабре.
5. В начале — краткое вступление (3-4 предложения о трендах недели)."""

# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    """Ленивая инициализация AsyncOpenAI-совместимого клиента (DeepSeek или OpenAI)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,   # DeepSeek: https://api.deepseek.com
            timeout=90.0,                # DeepSeek чуть медленнее OpenAI
            max_retries=2,
        )
        log.info("🔌 LLM клиент: {} ({})", cfg.llm_model, cfg.llm_base_url)
    return _client


# ---------------------------------------------------------------------------
# Приватные хелперы
# ---------------------------------------------------------------------------

async def _chat(
    system: str,
    user_prompt: str,
    max_tokens: int = 600,
    temperature: float = 0.4,
) -> Optional[str]:
    """
    Выполняет запрос к ChatCompletion.
    Возвращает текст ответа или None при ошибке.
    """
    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=cfg.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = response.choices[0].message.content
        if text:
            return text.strip()
        log.warning("⚠️  OpenAI вернул пустой ответ")
        return None

    except RateLimitError:
        log.error("🚫 OpenAI Rate Limit превышен — повторная попытка через 60 сек.")
        await asyncio.sleep(60)
        return None
    except APITimeoutError:
        log.error("⏱️  OpenAI Timeout")
        return None
    except APIError as exc:
        log.error("❌ OpenAI APIError: {}", exc)
        return None
    except Exception as exc:
        log.exception("💥 Неожиданная ошибка при запросе к OpenAI: {}", exc)
        return None


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

async def generate_post(
    title: str,
    raw_text: str,
    url: str,
    source: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Генерирует (ai_text, ai_short) для одной новости.

    Args:
        title:    Заголовок из RSS
        raw_text: Краткое описание/summary из RSS
        url:      Оригинальный URL статьи
        source:   Название источника (BleepingComputer и т.д.)

    Returns:
        (ai_text, ai_short) — оба могут быть None при ошибке API
    """
    user_prompt = (
        f"Источник: {source}\n"
        f"Заголовок: {title}\n"
        f"Текст: {raw_text}\n"
        f"URL: {url}\n\n"
        f"Напиши адаптированный пост для Telegram-канала по кибербезопасности."
    )

    short_prompt = (
        f"Заголовок: {title}\n"
        f"Краткое описание: {raw_text[:400]}\n\n"
        f"Напиши твит для X (до 240 символов)."
    )

    log.info("🤖 Генерация поста для: {}", title[:70])

    # Запускаем оба запроса параллельно
    ai_text, ai_short = await asyncio.gather(
        _chat(_SYSTEM_CYBERSEC, user_prompt, max_tokens=550, temperature=0.35),
        _chat(_SYSTEM_SHORT,    short_prompt, max_tokens=100, temperature=0.2),
    )

    # Обрезка по лимитам на случай если модель всё же превысила
    if ai_text and len(ai_text) > 950:
        ai_text = ai_text[:947] + "..."
    if ai_short and len(ai_short) > 250:
        ai_short = ai_short[:247] + "..."

    if ai_text:
        log.info("✅ ai_text сгенерирован ({} симв.)", len(ai_text))
    if ai_short:
        log.info("✅ ai_short сгенерирован ({} симв.)", len(ai_short))

    return ai_text, ai_short


async def generate_weekly_digest(news_rows: list) -> Optional[str]:
    """
    Генерирует Markdown-дайджест для Хабра из списка опубликованных новостей за неделю.

    Args:
        news_rows: Список sqlite3.Row объектов с полями title, ai_text, url

    Returns:
        Markdown-строка или None при ошибке
    """
    if not news_rows:
        log.warning("📭 Нет новостей для дайджеста")
        return None

    # Формируем список для промпта
    items = []
    for row in news_rows:
        items.append(
            f"- Заголовок: {row['title']}\n"
            f"  Текст: {(row['ai_text'] or row['raw_text'] or '')[:300]}\n"
            f"  URL: {row['url']}"
        )
    news_block = "\n\n".join(items)

    user_prompt = (
        f"Составь еженедельный дайджест кибербезопасности из следующих {len(news_rows)} новостей:\n\n"
        f"{news_block}\n\n"
        f"Формат: Markdown для Хабра."
    )

    log.info("📝 Генерация недельного дайджеста из {} новостей...", len(news_rows))
    digest = await _chat(_SYSTEM_DIGEST, user_prompt, max_tokens=2500, temperature=0.3)

    if digest:
        log.info("✅ Дайджест сгенерирован ({} симв.)", len(digest))
    else:
        log.error("❌ Не удалось сгенерировать дайджест")

    return digest
