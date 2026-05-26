"""
database.py — Инициализация SQLite и CRUD-операции для таблицы news.

Схема таблицы news:
  id          INTEGER PRIMARY KEY AUTOINCREMENT
  title       TEXT NOT NULL
  raw_text    TEXT             — Оригинальный текст из RSS
  ai_text     TEXT             — Рерайт для Telegram/VK (до ~1000 символов)
  ai_short    TEXT             — Короткая версия для X/Threads (до 250 символов)
  url         TEXT UNIQUE NOT NULL — Оригинальная ссылка (дедупликация)
  source      TEXT             — Имя источника RSS
  status      TEXT DEFAULT 'pending'  — pending | approved | rejected | published
  is_viral    INTEGER DEFAULT 0       — 1 если отмечена кнопкой 🔥
  created_at  TEXT DEFAULT (datetime('now'))
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from logger import log


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Контекстный менеджер соединения с row_factory."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")   # Повышает конкурентность
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Создаёт таблицы, если они ещё не существуют."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS news (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT    NOT NULL,
                    raw_text    TEXT,
                    ai_text     TEXT,
                    ai_short    TEXT,
                    url         TEXT    UNIQUE NOT NULL,
                    source      TEXT,
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    is_viral    INTEGER NOT NULL DEFAULT 0,
                    published_at TEXT,
                    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key         TEXT    PRIMARY KEY,
                    value       TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_news_status     ON news(status);
                CREATE INDEX IF NOT EXISTS idx_news_created_at ON news(created_at);
                CREATE INDEX IF NOT EXISTS idx_news_is_viral   ON news(is_viral);
            """)
            
            # Миграция: добавляем колонку published_at, если база данных была создана ранее
            try:
                conn.execute("ALTER TABLE news ADD COLUMN published_at TEXT;")
                log.info("💾 Схема БД обновлена: добавлена колонка published_at")
            except sqlite3.OperationalError:
                pass  # Колонка уже существует

            # По умолчанию автоматический парсинг включен
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_parser_enabled', '1')"
            )
        log.info("✅ База данных инициализирована: {}", self.db_path)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def url_exists(self, url: str) -> bool:
        """Проверяет, есть ли уже запись с таким URL (дедупликация)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM news WHERE url = ?", (url,)
            ).fetchone()
            return row is not None

    def insert_news(
        self,
        title: str,
        raw_text: str,
        url: str,
        source: str,
        published_at: Optional[str] = None,
    ) -> Optional[int]:
        """
        Вставляет новую запись со статусом 'pending'.
        Возвращает id вставленной записи или None при дубле.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO news (title, raw_text, url, source, published_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (title, raw_text, url, source, published_at),
                )
                news_id = cursor.lastrowid
                log.debug("📥 Новость сохранена [id={}]: {}", news_id, title[:80])
                return news_id
        except sqlite3.IntegrityError:
            # URL уже существует — дубликат
            log.debug("⏭️  Дубликат, пропуск: {}", url)
            return None

    def update_ai_content(
        self,
        news_id: int,
        ai_text: str,
        ai_short: str,
    ) -> None:
        """Сохраняет результат LLM-обработки."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE news SET ai_text = ?, ai_short = ? WHERE id = ?",
                (ai_text, ai_short, news_id),
            )
        log.debug("🤖 AI-контент обновлён [id={}]", news_id)

    def set_status(self, news_id: int, status: str) -> None:
        """Устанавливает статус: pending | approved | rejected | published."""
        valid = {"pending", "approved", "rejected", "published"}
        if status not in valid:
            raise ValueError(f"Недопустимый статус: {status}. Допустимо: {valid}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE news SET status = ? WHERE id = ?",
                (status, news_id),
            )
        log.info("📌 Статус новости [id={}] → {}", news_id, status)

    def set_viral(self, news_id: int, is_viral: bool = True) -> None:
        """Помечает новость как вирусную."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE news SET is_viral = ? WHERE id = ?",
                (1 if is_viral else 0, news_id),
            )
        log.info("🔥 Вирусный флаг [id={}] → {}", news_id, is_viral)

    def get_by_id(self, news_id: int) -> Optional[sqlite3.Row]:
        """Возвращает запись по ID."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM news WHERE id = ?", (news_id,)
            ).fetchone()

    def get_published_since(self, days: int = 7) -> list[sqlite3.Row]:
        """Возвращает опубликованные записи за последние N дней (для дайджеста)."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM news
                WHERE status = 'published'
                  AND created_at >= datetime('now', ? || ' days')
                ORDER BY created_at DESC
                """,
                (f"-{days}",),
            ).fetchall()

    def get_pending_with_ai(self) -> list[sqlite3.Row]:
        """Возвращает записи, готовые к модерации (есть ai_text, статус pending)."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM news
                WHERE status = 'pending' AND ai_text IS NOT NULL
                ORDER BY created_at ASC
                """
            ).fetchall()

    def get_pending_ids(self) -> list:
        """Возвращает ID статей без ai_text (ждут LLM-обработки)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM news
                WHERE status = 'pending' AND (ai_text IS NULL OR ai_text = '')
                ORDER BY created_at ASC
                LIMIT 50
                """
            ).fetchall()
            return [r["id"] for r in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        """Получить значение настройки из БД."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Установить значение настройки в БД."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        log.info("⚙️ Настройка БД [{}] → {}", key, value)
