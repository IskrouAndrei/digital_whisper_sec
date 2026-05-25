"""
logger.py — Централизованная настройка loguru.
Импортируй `log` из этого модуля во всех файлах проекта.

Использование:
    from logger import log
    log.info("Парсер запущен")
    log.error("Ошибка подключения к БД: {}", exc)
"""

import sys
from loguru import logger as _logger


def setup_logger(log_file_path: str = "logs/bot.log", log_level: str = "INFO") -> None:
    """
    Настраивает loguru:
      - Консольный вывод (INFO+) с цветами
      - Файловый вывод с ротацией 10 MB, хранением 7 дней, сжатием zip
    Вызывается один раз в main.py перед стартом бота.
    """
    # Убираем дефолтный обработчик loguru
    _logger.remove()

    # === Консольный sink (STDOUT) ===
    _logger.add(
        sys.stdout,
        level=log_level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        backtrace=True,
        diagnose=True,
    )

    # === Файловый sink ===
    _logger.add(
        log_file_path,
        level=log_level,
        rotation="10 MB",       # Ротация при достижении 10 MB
        retention="7 days",     # Хранить логи 7 дней
        compression="zip",      # Сжимать старые файлы
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{function}:{line} — {message}"
        ),
    )

    _logger.info("📋 Логгер инициализирован. Файл: {}, уровень: {}", log_file_path, log_level)


# Экспортируемый объект логгера
log = _logger
