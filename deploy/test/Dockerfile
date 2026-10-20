# Окружение прогона проверок (compose.test.yaml): uv, Python 3.11 и git для квитанции исходников.
# Исходники в образ не копируются — их подключает compose, поэтому образ не пересобирается
# при каждом изменении кода. Версии — как в корневом Dockerfile и CI.
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /work
