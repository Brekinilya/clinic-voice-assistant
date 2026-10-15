# Asterisk для голосового шлюза

Звонок на номер 100 попадает в ARI-приложение `clinic`. Голосовой шлюз (`app/voice/telephony.py`)
отвечает, соединяет пациента со своим UDP-портом через внешний медиаканал и ведёт разговор так же,
как звонок из браузера. Перевод на регистратора — вызов софтфона `operator` в тот же мост.

Звук внешнего медиаканала — ulaw 8 кГц: Asterisk 20 из пакетов Ubuntu не принимает от шлюза
slin16. Для голоса это обычное качество телефонной линии.

## Пароли

В `.env` в корне репозитория (в git не попадает):

```
ARI_USER=clinic
ARI_PASSWORD=…
SIP_PATIENT_PASSWORD=…
SIP_OPERATOR_PASSWORD=…
# Адрес, по которому софтфоны видят Asterisk в контейнере; для WSL и ВМ не нужен
SIP_EXTERNAL_ADDRESS=
```

## Ubuntu: WSL или ВМ

```bash
sudo apt-get install asterisk asterisk-modules asterisk-moh-opsound-gsm gettext-base
sudo sh deploy/asterisk/install.sh
```

`install.sh` кладёт конфигурацию из `config/` с паролями из `.env` в `/etc/asterisk` и перезапускает
Asterisk. Голосовой стенд с телефонией:

```bash
uv sync --locked --extra voice
BACKEND_DEMO_ENABLED=true DIALOGUE_MODE=natural TELEPHONY_ENABLED=true \
TELEPHONY_MEDIA_HOST=127.0.0.1 uv run uvicorn app.voice.server:create_voice_app --factory
```

## Docker

```bash
docker compose -f deploy/asterisk/compose.yaml up --build
```

Шлюз на том же компьютере: `TELEPHONY_MEDIA_HOST=host.docker.internal`, порты RTP Asterisk
10000–10099 опубликованы.

## Софтфоны

Две учётные записи: `patient` — звонит в регистратуру, `operator` — принимает переводы. Сервер —
адрес машины с Asterisk (в WSL: `hostname -I` внутри Ubuntu), порт 5060, UDP, кодеки G.722 или
PCMU. Номер регистратуры — 100, проверить звонок регистратору — 200.

## Проверка без софтфона

`call_test.py` звонит на 100 Local-каналом, ждёт, пока ассистент договорит, и проигрывает реплики
из `data/telephony/sounds/*.wav16` (16 кГц). Разговор в обе стороны пишется в
`/var/spool/asterisk/recording/<имя>.wav`.

```bash
python deploy/asterisk/call_test.py want name first yes no
python deploy/asterisk/call_test.py want barge:name
```
