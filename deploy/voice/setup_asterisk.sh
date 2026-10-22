#!/bin/bash
# Asterisk на хосте под SIP-шлюз хакатона и голосовой шлюз в контейнере.
# Из корня репозитория: bash deploy/voice/setup_asterisk.sh
#
# Пароли не печатаются: SIP — из комплекта организаторов (SIP_DIR, по умолчанию
# /opt/hackathon/sip), ARI — в deploy/voice/voice.env (создаётся один раз, права 600).
# Прежние файлы пакета сохраняются в /etc/asterisk/.package-defaults при первом запуске.
# На ВМ, где Asterisk уже настроен, скрипт перезаписывает его конфигурацию — запускать
# только в согласованный выпуск, без активных звонков.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SIP_DIR="${SIP_DIR:-/opt/hackathon/sip}"
NUMBER="${CLINIC_NUMBER:-021}"
# Адрес шлюза сети clinic-vm_default: на нём ARI доступен контейнеру голоса
GATEWAY="${COMPOSE_GATEWAY:-$(docker network inspect clinic-vm_default \
    --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.18.0.1)}"
ENV_FILE="$HERE/voice.env"

if [ ! -f "$ENV_FILE" ]; then
    umask 077
    sed "s/^ARI_PASSWORD=.*/ARI_PASSWORD=$(openssl rand -hex 24)/" \
        "$HERE/voice.env.example" > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
ARI_USER="$(grep -E '^ARI_USER=' "$ENV_FILE" | cut -d= -f2-)"
ARI_PASSWORD="$(grep -E '^ARI_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
[ -n "$ARI_PASSWORD" ] || { echo "в $ENV_FILE пустой ARI_PASSWORD" >&2; exit 1; }

if ! command -v asterisk >/dev/null; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq asterisk >/dev/null
fi
asterisk -V

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Регистрация на шлюзе и модули — из комплекта организаторов, без изменений
cp "$SIP_DIR/pjsip.conf" "$SIP_DIR/modules.conf" "$work/"

cat > "$work/extensions.conf" <<CONF
; Входящий с SIP-шлюза хакатона: номер $NUMBER — регистратура, разговор ведёт голосовой шлюз.
; Исходящие напоминания T11 голос создаёт через ARI (originate), а не через dialplan.
[general]
static = yes
writeprotect = yes

[from-gateway]
exten => $NUMBER,1,NoOp(Регистратура)
 same => n,Set(CALLBACK=)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-1"]?expert1)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-2"]?expert2)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-3"]?expert3)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-4"]?expert4)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-5"]?expert5)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "expert-6"]?expert6)
 same => n,GotoIf(\$["\${CALLERID(num)}" = "phone-021"]?selftest)
 same => n,Set(SELFTEST_ID=\${FILTER(0-9A-Za-z,\${CALLERID(num)})})
 same => n,GotoIf(\$["\${SELFTEST_ID}" = "phone021"]?selftest)
 same => n(inbound),Stasis(clinic,inbound)
 same => n,Hangup()
 same => n(expert1),Set(CALLBACK=expert-1)
 same => n,Goto(inbound)
 same => n(expert2),Set(CALLBACK=expert-2)
 same => n,Goto(inbound)
 same => n(expert3),Set(CALLBACK=expert-3)
 same => n,Goto(inbound)
 same => n(expert4),Set(CALLBACK=expert-4)
 same => n,Goto(inbound)
 same => n(expert5),Set(CALLBACK=expert-5)
 same => n,Goto(inbound)
 same => n(expert6),Set(CALLBACK=expert-6)
 same => n,Goto(inbound)
 same => n(selftest),Set(CALLBACK=phone-021)
 same => n,Goto(inbound)

; Hang up only after Stasis returns; call back only whitelisted experts or our test phone.
exten => h,1,GotoIf(\$["\${CALLBACK}" = ""]?done)
 same => n,NoOp(Calling back the caller who reached this extension)
 same => n,Originate(PJSIP/\${CALLBACK}@gateway,exten,reminder-answered,s,1,45,a)
 same => n(done),NoOp(No callback required)

; Проверочный исходящий на софтфон команды — из комплекта организаторов, как на ВМ
[reminders]
exten => call,1,Dial(PJSIP/phone-$NUMBER@gateway,45)
 same => n,Hangup()

[reminder-answered]
exten => s,1,Answer()
 same => n,Wait(1)
 same => n,Playback(hello-world)
 same => n,Hangup()
CONF

cat > "$work/http.conf" <<CONF
; ARI только для голосового шлюза в сети compose; 8088 на ВМ занят файловым менеджером.
[general]
enabled = yes
bindaddr = $GATEWAY
bindport = 8090
CONF

cat > "$work/ari.conf" <<CONF
[general]
enabled = yes
pretty = no

[$ARI_USER]
type = user
read_only = no
password = $ARI_PASSWORD
password_format = plain
CONF

cat > "$work/rtp.conf" <<'CONF'
; Порты RTP, до которых достаёт шлюз хакатона. Без ICE: иначе Asterisk отбрасывает RTP шлюза.
[general]
rtpstart = 10000
rtpend = 20000
icesupport = no
CONF

if [ ! -d /etc/asterisk/.package-defaults ]; then
    sudo mkdir /etc/asterisk/.package-defaults
    for f in pjsip extensions modules http ari rtp; do
        [ -f "/etc/asterisk/$f.conf" ] && sudo cp -p "/etc/asterisk/$f.conf" /etc/asterisk/.package-defaults/
    done
fi
for f in pjsip extensions modules http ari rtp; do
    sudo install -m 0640 -o asterisk -g asterisk "$work/$f.conf" "/etc/asterisk/$f.conf"
done

# ARI слушает адрес сети Docker: после перезагрузки Asterisk стартует после Docker
sudo mkdir -p /etc/systemd/system/asterisk.service.d
printf '[Unit]\nAfter=docker.service\nWants=docker.service\n' \
    | sudo tee /etc/systemd/system/asterisk.service.d/after-docker.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q asterisk
sudo systemctl restart asterisk
sleep 20
sudo asterisk -rx "pjsip show registrations" | tail -3
sudo asterisk -rx "http show status" | grep -iE "enabled|bound"
