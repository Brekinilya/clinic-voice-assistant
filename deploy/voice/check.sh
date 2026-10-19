#!/bin/bash
# Проверка установленного голосового контура — только чтение, без звонков и изменений.
# Из корня репозитория: bash deploy/voice/check.sh [каталог моделей]
# CLINIC_ROOT, VOICE_ENV, VOICE_MODELS_HOST переопределяют пути (например, для ~/voice на ВМ).
# Печатает ок/НЕТ по каждому пункту и итог; значения секретов не выводятся.
set -u
cd "${CLINIC_ROOT:-$(dirname "$0")/../..}" || exit 2
MODELS="${1:-${VOICE_MODELS_HOST:-models}}"
VOICE_ENV="${VOICE_ENV:-deploy/voice/voice.env}"
failed=0
check() {
    if eval "$2" >/dev/null 2>&1; then echo "ок   $1"; else echo "НЕТ  $1"; failed=$((failed + 1)); fi
}

echo "== код"
head=$(git rev-parse HEAD 2>/dev/null || echo unknown)
image=$(docker inspect clinic-voice-voice-1 --format '{{.Image}}' 2>/dev/null)
built=$(docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)
echo "репозиторий $head"
echo "образ голоса ${built:-без метки}"
check "образ собран из текущего коммита" '[ "$built" = "$head" ]'
check "в рабочем дереве нет незакоммиченных правок" '[ -z "$(git status --porcelain -- app config web pyproject.toml uv.lock)" ]'

echo "== модели ($MODELS)"
check "хеши моделей совпадают с deploy/voice/models.sha256" \
    '(sums="$PWD/deploy/voice/models.sha256"; cd "$MODELS" && sha256sum --quiet -c "$sums")'

echo "== файлы с секретами"
for f in .env "$VOICE_ENV"; do
    check "$f существует и закрыт (600)" '[ "$(stat -c %a "$f")" = 600 ]'
done
check "voice.env не попадает в образ" 'grep -qx "deploy/voice/voice.env" .dockerignore'

echo "== контейнеры"
for name in clinic-vm-database-1 clinic-vm-app-1 clinic-voice-voice-1; do
    check "$name healthy" '[ "$(docker inspect "$name" --format "{{.State.Health.Status}}")" = healthy ]'
done
for name in clinic-vm-recovery-1 clinic-vm-schedule-updater-1; do
    check "$name запущен" '[ "$(docker inspect "$name" --format "{{.State.Running}}")" = true ]'
done
check "backend /health/ready" 'curl -fsS -m 3 http://127.0.0.1:8000/health/ready'
check "voice /health/ready" 'curl -fsS -m 3 http://127.0.0.1:8001/health/ready'
check "голос наружу не открыт (только 127.0.0.1:8001)" \
    '[ "$(docker port clinic-voice-voice-1 8001/tcp)" = "127.0.0.1:8001" ]'

echo "== база"
set -a; . ./.env 2>/dev/null; set +a
expected=$(grep -oE '^SCHEMA_VERSION = [0-9]+' app/storage/schema.py | grep -oE '[0-9]+$')
actual=$(docker exec clinic-vm-database-1 psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
    -c "select version from backend_schema" 2>/dev/null)
echo "схема: ожидается $expected, в базе ${actual:-нет}"
check "миграция применена" '[ "$actual" = "$expected" ]'

echo "== Asterisk"
check "служба asterisk активна" 'systemctl is-active --quiet asterisk'
check "регистрация на SIP-шлюзе" 'sudo asterisk -rx "pjsip show registrations" | grep -q Registered'
check "голос подключён к ARI (приложение clinic)" 'sudo asterisk -rx "ari show apps" | grep -qw clinic'
check "ARI слушает только сеть compose, не 0.0.0.0" \
    '! sudo ss -ltn | grep -E "0\.0\.0\.0:8090|\*:8090"'

echo "== напоминания T11"
enabled=$(grep -E '^REMINDERS_ENABLED=' "$VOICE_ENV" 2>/dev/null | cut -d= -f2)
echo "REMINDERS_ENABLED=${enabled:-не задано} (только в голосовом процессе)"
check "в backend обзвон не включён" '! grep -qE "^REMINDERS_ENABLED=true" .env'

echo "== итог: $failed проверок не прошли"
exit $((failed > 0))
