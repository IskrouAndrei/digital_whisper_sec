FROM python:3.11-slim

# Метаданные образа
LABEL maintainer="CyberSentry Team"
LABEL description="CyberSentry: The Digital Whisper — Omni-Channel CyberSec AI Aggregator"

# Не буферизировать Python stdout/stderr (важно для логов в Docker)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Рабочая директория
WORKDIR /app

# Устанавливаем системные зависимости (для lxml и других нативных пакетов)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем и устанавливаем зависимости отдельным слоем (кэш)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения
COPY . .

# Создаём папки для данных (будут перекрыты volumes)
RUN mkdir -p /data /logs

# Открываем именованные volumes как точки монтирования
VOLUME ["/data", "/logs"]

# Точка входа
CMD ["python", "main.py"]
