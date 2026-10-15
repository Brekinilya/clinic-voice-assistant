#!/bin/sh
# Пароли и адреса — из окружения (.env), в репозитории их нет.
set -eu
sh /opt/clinic/render.sh /opt/clinic/asterisk /etc/asterisk
chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk 2>/dev/null || true
exec asterisk -f -U asterisk -G asterisk -vvv
