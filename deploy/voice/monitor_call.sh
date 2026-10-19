#!/bin/bash
# Наблюдение за живым звонком — только чтение: ресурсы, RTP на SIP-плече, задержки по репликам.
# Запуск до звонка: bash deploy/voice/monitor_call.sh [секунд=300] [интервал=2] > протокол.log
# Тексты разговора и аудио не собираются: голос пишет в журнал только числа задержек,
# Asterisk — счётчики пакетов. Вывод хранить вне Git.
set -u
duration="${1:-300}"
interval="${2:-2}"
voice="${VOICE_CONTAINER:-clinic-voice-voice-1}"
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
stamp() { date +%H:%M:%S; }

echo "# начало $(date -Is), длительность ${duration} с, интервал ${interval} с"
image=$(docker inspect "$voice" --format '{{.Image}}' 2>/dev/null)
echo "# образ голоса $(docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)"
echo "# $(asterisk -V 2>/dev/null)"
echo "# ресурсы: $(nproc) CPU, $(free -m | awk 'NR==2{print $2}') МБ RAM"

end=$((SECONDS + duration))
while [ "$SECONDS" -lt "$end" ]; do
    now=$(stamp)
    docker stats --no-stream --format "$now stats {{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}" \
        "$voice" clinic-vm-app-1 clinic-vm-database-1 2>/dev/null
    channels=$(sudo asterisk -rx "core show channels concise" 2>/dev/null)
    echo "$now channels $(printf '%s\n' "$channels" | grep -c '!')"
    # SIP-плечо шлюз ↔ Asterisk: принятые/потерянные пакеты, джиттер, RTT
    if printf '%s\n' "$channels" | grep -q '^PJSIP/'; then
        # Формат таблицы зависит от версии Asterisk: выводим её целиком, без пустых строк
        sudo asterisk -rx "pjsip show channelstats" 2>/dev/null \
            | grep -vE '^[[:space:]]*$|^=+$' | sed "s/^/$now rtp /"
    fi
    sleep "$interval"
done

echo "# задержки по репликам из журнала голоса (с $since)"
docker logs --since "$since" "$voice" 2>&1 | grep -E 'задержки' | sed -E 's/^.*звонок /call /'
echo "# ошибки голоса: $(docker logs --since "$since" "$voice" 2>&1 | grep -cE ' ERROR |Traceback')"
echo "# конец $(date -Is)"
