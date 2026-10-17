#!/bin/sh
# Шаблоны конфигурации → каталог Asterisk. Подставляются только наши переменные окружения:
# ${...} в диалплане — переменные самого Asterisk.
# sh render.sh <шаблоны> <каталог Asterisk>
set -eu
source_dir=$1
target_dir=$2

: "${ARI_USER:?нужен ARI_USER}"
: "${ARI_PASSWORD:?нужен ARI_PASSWORD}"
: "${SIP_PATIENT_PASSWORD:?нужен SIP_PATIENT_PASSWORD}"
: "${SIP_OPERATOR_PASSWORD:?нужен SIP_OPERATOR_PASSWORD}"
: "${SIP_EXTERNAL_ADDRESS:=127.0.0.1}"
export ARI_USER ARI_PASSWORD SIP_PATIENT_PASSWORD SIP_OPERATOR_PASSWORD SIP_EXTERNAL_ADDRESS

VARIABLES='$ARI_USER $ARI_PASSWORD $SIP_PATIENT_PASSWORD $SIP_OPERATOR_PASSWORD $SIP_EXTERNAL_ADDRESS'
for template in "$source_dir"/*.conf; do
    envsubst "$VARIABLES" < "$template" > "$target_dir/$(basename "$template")"
done
