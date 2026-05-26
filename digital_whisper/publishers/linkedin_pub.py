"""
publishers/linkedin_pub.py — Публикация в LinkedIn через REST API v2 (Posts).

Поддерживает:
  - LINKEDIN_USER_ID  (числовой ID или URN — приоритет)
  - LINKEDIN_PERSON_URN (устаревшее имя, fallback)

Ключи берутся только из окружения (.env на сервере), не из кода.
Пропускает публикацию, если токены не настроены (graceful degradation).
"""

import re
import aiohttp
from typing import Any, Optional

from config import cfg
from logger import log

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Убирает HTML-теги — LinkedIn не поддерживает HTML в тексте поста."""
    clean = re.sub(r"<[^>]+>", "", text)
    # Убираем лишние пустые строки после удаления тегов
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _get_person_urn() -> Optional[str]:
    """
    Определяет URN автора публикации.
    Приоритет: LINKEDIN_USER_ID → LINKEDIN_PERSON_URN.
    Поддерживает числовые ID и полные URN-строки.
    """
    # Читаем оба варианта (с зачисткой inline-комментариев)
    user_id = (cfg.linkedin_user_id or "").split("#")[0].strip()
    person_urn = (cfg.linkedin_person_urn or "").split("#")[0].strip()

    raw = user_id or person_urn
    if not raw:
        return None

    # Уже полный URN — возвращаем как есть
    if raw.startswith("urn:li:"):
        return raw

    # Числовой ID или строковый — оборачиваем в person URN
    return f"urn:li:person:{raw}"


def _build_post_text(row: Any) -> str:
    """Формирует текст поста для LinkedIn (без HTML, с хэштегами)."""
    # LinkedIn не рендерит HTML — очищаем
    clean_text = _strip_html(row["ai_text"] or "")

    viral_badge = "\n\n🔥 Trending" if row.get("is_viral") else ""
    hashtags = "#cybersecurity #infosec #threatintel"

    return (
        f"{clean_text}"
        f"{viral_badge}\n\n"
        f"🔗 {row['url']}\n\n"
        f"{hashtags}"
    )


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

async def publish_to_linkedin(row: Any) -> bool:
    """
    Публикует пост в LinkedIn от имени личного аккаунта.

    Returns:
        True  — успешно опубликовано ИЛИ платформа не настроена (пропуск).
        False — ошибка API при попытке публикации.
    """
    row = dict(row)
    # Токен доступа
    access_token = (cfg.linkedin_access_token or "").split("#")[0].strip()
    if not access_token or access_token.startswith("your_"):
        log.info("⏭️  [LinkedIn] Токен не настроен — пропускаем")
        return True

    # URN автора
    author_urn = _get_person_urn()
    if not author_urn:
        log.info("⏭️  [LinkedIn] LINKEDIN_USER_ID / LINKEDIN_PERSON_URN не заданы — пропускаем")
        return True

    if not row.get("ai_text"):
        log.error("❌ [LinkedIn] ai_text пустой для новости #{}", row["id"])
        return False

    text = _build_post_text(row)
    log.info("📢 [LinkedIn] Публикация новости #{}... (author={})", row["id"], author_urn)

    # LinkedIn Posts API v2 — актуальный эндпоинт
    payload = {
        "author": author_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "content": {
            "article": {
                "source": row["url"],
                "title": row.get("title", "")[:200],  # LinkedIn: макс 200 символов в заголовке
            }
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": "202605",   # Актуальная версия API
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.linkedin.com/rest/posts",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status in (200, 201):
                    post_id = response.headers.get("x-restli-id", "unknown")
                    log.info("✅ [LinkedIn] Новость #{} опубликована. Post ID: {}", row["id"], post_id)
                    return True

                error_body = await response.text()
                log.error(
                    "❌ [LinkedIn] Ошибка API. Статус: {}, Ответ: {:.500}",
                    response.status,
                    error_body,
                )

                # 401 — токен истёк или неверный
                if response.status == 401:
                    log.error("❌ [LinkedIn] Токен недействителен или истёк. Обнови LINKEDIN_ACCESS_TOKEN.")
                # 403 — нет прав (scope)
                elif response.status == 403:
                    log.error(
                        "❌ [LinkedIn] Нет прав на публикацию. "
                        "Убедись что в LinkedIn App включены scopes: w_member_social, r_liteprofile"
                    )

                return False

    except aiohttp.ClientConnectorError as exc:
        log.error("❌ [LinkedIn] Ошибка соединения: {}", exc)
        return False
    except aiohttp.ServerTimeoutError:
        log.error("❌ [LinkedIn] Timeout при запросе к LinkedIn API")
        return False
    except Exception as exc:
        log.exception("💥 [LinkedIn] Неожиданная ошибка: {}", exc)
        return False
