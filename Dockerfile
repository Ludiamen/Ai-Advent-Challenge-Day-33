# Мини-сервис поддержки пользователей портала ИТ-отдела (День 33, plan D-10).
# Образ собирается ДОМА (там есть index-support/chunks.json — фрагменты документации CRM вне git)
# и переносится на VPS: docker save … | ssh … docker load. См. DEPLOY.md.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 support

WORKDIR /app
COPY requirements-support.txt .
RUN pip install --no-cache-dir -r requirements-support.txt

COPY agent/ agent/
COPY support_app.py support_server.py support-seed.json ./
COPY support/ support/
COPY templates/support.html templates/support_desk.html templates/
COPY index-support/chunks.json index-support/chunks.json

ENV SUPPORT_DATA=/data/desk.json \
    MEMORY_DIR=/data \
    SUPPORT_KB=/app/index-support/chunks.json \
    SUPPORT_LOCAL=0 \
    SUPPORT_PORT=8450 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data && chown support:support /data
USER support
VOLUME /data
EXPOSE 8450

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD python -c "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8450/api/support/health', context=ssl._create_unverified_context(), timeout=5)" || exit 1

CMD ["python", "support_app.py"]
