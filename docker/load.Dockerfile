# Клиент нагрузки Дня 30: чистый Python с httpx и openai — без кода проекта.
FROM python:3.12-slim
RUN pip install --no-cache-dir httpx openai
WORKDIR /app
