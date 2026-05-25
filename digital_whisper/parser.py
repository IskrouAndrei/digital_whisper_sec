"""
parser.py — Асинхронный мониторинг RSS-лент кибербезопасности.

Логика работы:
  1. Для каждого URL из cfg.rss_feeds делаем feedparser.parse() в executor
     (feedparser — синхронный, запускаем в ThreadPoolExecutor).
  2. Проверяем каждую новость на дубликат по URL через db.url_exists().
  3. Новые статьи сохраняем в БД со статусом 'pending'.
  4. Возвращаем список ID новых записей для дальнейшей LLM-обработки.
"""

from __future__ import annotations

import asyncio
import html
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import feedparser

from config import cfg
from database import Database
from logger import log

# Пул потоков для feedparser (CPU/IO-bound синхронная библиотека)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rss_worker")

# RSS-ленты кибербезопасности
RSS_SOURCES: dict[str, str] = {
    "The Hacker News":       "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer":      "https://www.bleepingcomputer.com/feed/",
    "Securelist":            "https://securelist.com/feed/",
    "Unit 42":               "https://unit42.paloaltonetworks.com/feed/",
    "Dark Reading":          "https://www.darkreading.com/rss.xml",
    "Krebs on Security":     "https://krebsonsecurity.com/feed/",
}


def _clean_html(text: str) -> str:
    """Убираем HTML-теги и декодируем HTML-сущности из summary."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _fetch_feed_sync(url: str) -> Optional[feedparser.FeedParserDict]:
    """Синхронная загрузка и парсинг RSS (вызывается в executor)."""
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            log.warning("⚠️  Bozo-флаг для {}: {}", url, feed.bozo_exception)
            return None
        return feed
    except Exception as exc:
        log.error("❌ Ошибка загрузки RSS {}: {}", url, exc)
        return None


async def fetch_feed(url: str) -> Optional[feedparser.FeedParserDict]:
    """Асинхронная обёртка над синхронным feedparser."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_feed_sync, url)


async def parse_and_store(db: Database) -> list[int]:
    """
    Основная функция парсера:
      - Обходит все RSS-источники параллельно
      - Сохраняет новые статьи в БД
      - Возвращает список ID новых записей
    """
    # Строим задачи для параллельного запроса
    feeds_to_check = cfg.rss_feeds if cfg.rss_feeds else list(RSS_SOURCES.values())
    source_map = {v: k for k, v in RSS_SOURCES.items()}

    tasks = {url: asyncio.create_task(fetch_feed(url)) for url in feeds_to_check}
    new_ids: list[int] = []

    log.info("🔍 Запускаем парсинг {} RSS-источников...", len(tasks))

    for url, task in tasks.items():
        source_name = source_map.get(url, url)
        feed = await task

        if feed is None:
            log.warning("🚫 Источник недоступен: {}", source_name)
            continue

        entries = feed.get("entries", [])
        log.info("📰 {} → {} записей в ленте", source_name, len(entries))

        new_count = 0
        for entry in entries:
            entry_url = entry.get("link", "").strip()
            if not entry_url:
                continue

            # Дедупликация
            if db.url_exists(entry_url):
                continue

            title = entry.get("title", "Без заголовка").strip()
            summary = _clean_html(entry.get("summary", entry.get("description", "")))

            news_id = db.insert_news(
                title=title,
                raw_text=summary,
                url=entry_url,
                source=source_name,
            )

            if news_id is not None:
                new_ids.append(news_id)
                new_count += 1

        if new_count:
            log.info("✅ {} → {} новых статей сохранено", source_name, new_count)
        else:
            log.debug("⏭️  {} → нет новых статей", source_name)

    log.info("📊 Парсинг завершён. Всего новых статей: {}", len(new_ids))
    return new_ids
