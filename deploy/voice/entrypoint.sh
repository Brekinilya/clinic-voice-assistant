#!/bin/sh
# Адрес контейнера в сети compose: по нему Asterisk на хосте шлёт звук пациента.
set -eu
if [ -z "${TELEPHONY_MEDIA_HOST:-}" ]; then
    TELEPHONY_MEDIA_HOST="$(hostname -i | awk '{print $1}')"
    export TELEPHONY_MEDIA_HOST
fi
exec uvicorn app.voice.server:create_voice_app --factory \
    --host 0.0.0.0 --port 8001 --no-access-log --log-config /etc/clinic/log.json
