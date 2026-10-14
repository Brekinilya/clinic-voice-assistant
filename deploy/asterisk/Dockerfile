# Asterisk из пакетов Ubuntu 24.04 (ветка 20 LTS): PJSIP, ARI и внешний медиаканал для голосового шлюза.
FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        asterisk asterisk-modules asterisk-moh-opsound-gsm gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY config/ /opt/clinic/asterisk/
COPY render.sh /opt/clinic/render.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 5060/udp 8088/tcp
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
