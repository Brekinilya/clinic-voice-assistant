#!/bin/sh
# Asterisk на Linux-машине (WSL или ВМ) из пакетов Ubuntu: наши конфиги с паролями из .env.
#   sudo apt-get install asterisk asterisk-modules asterisk-moh-opsound-gsm gettext-base
#   sudo sh deploy/asterisk/install.sh [путь к .env]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
env_file=${1:-$here/../../.env}

set -a
. "$env_file"
set +a
sh "$here/render.sh" "$here/config" /etc/asterisk
chown asterisk:asterisk /etc/asterisk/*.conf
chmod 0640 /etc/asterisk/ari.conf /etc/asterisk/pjsip.conf

# Реплики «пациента» для call_test.py: data/telephony/sounds/*.wav16
sounds=$here/../../data/telephony/sounds
if [ -d "$sounds" ]; then
    ln -sfn "$(cd "$sounds" && pwd)" /var/lib/asterisk/sounds/clinic
fi

systemctl restart asterisk
