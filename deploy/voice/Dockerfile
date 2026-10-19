# Голосовой шлюз: тот же код, что у backend, плюс группа voice (VAD, ASR, TTS на CPU).
# Сборка из корня репозитория: docker compose -f deploy/voice/compose.yaml build
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /bin/uv
WORKDIR /app

# Отдельный слой зависимостей: по uv.lock, без пересборки при каждом изменении кода
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra voice --no-install-project
# Модели, данные и .env в образ не попадают (.dockerignore): они монтируются
COPY . .
RUN uv sync --frozen --no-dev --extra voice --no-editable \
    && useradd --uid 10001 --create-home appuser
COPY --chmod=0755 deploy/voice/entrypoint.sh /usr/local/bin/voice-entrypoint

# Какой коммит слушает телефон: проверка и протокол звонков читают эту метку
ARG SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT

USER appuser
EXPOSE 8001

# Модели грузятся и прогреваются до первого звонка: на 2 vCPU это около минуты
HEALTHCHECK --interval=30s --timeout=3s --start-period=90s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=2)"

CMD ["voice-entrypoint"]
