# Используем стабильный Python 3.11 (3.13 несовместим с aiogram)
FROM python:3.11-slim

# Устанавливаем зависимости системы
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Указываем переменную окружения (Render сам подставит TOKEN)
ENV PYTHONUNBUFFERED=1

# Запуск бота
CMD ["python", "bot.py"]
