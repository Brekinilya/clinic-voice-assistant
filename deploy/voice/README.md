# Голосовой контур: телефон → Asterisk → голосовой шлюз → диалог

Рецепт полного стека для ВМ хакатона: backend и база из `deploy/vm`, голос и Asterisk отсюда.

**Насколько он проверен.** Рецепт восстановлен по работающей ВМ (снимок 19.10.2026: `main`
c537fb7, схема 9, контейнеры healthy, регистрация Registered): порты, сеть, переменные,
`ari.conf`, `http.conf`, `rtp.conf`, dialplan и хеши моделей сверены с ней. `check.sh`
прогнан на этой ВМ только на чтение. `compose.yaml` проходит `docker compose config`.

Образ по этому `Dockerfile` собран локально (Docker 29.1, 19.10): около 980 МБ, метка коммита
на месте, внутри нет `.env`, `voice.env` и моделей, процесс от UID 10001. Запуск с этими
моделями без телефонии и без PostgreSQL: `healthy` через 14 с, `/health/ready` — 503 из-за
отсутствующей базы (так и должно быть), WebSocket получил приветствие (`ready`, `speak` и
звук), около 1,2 ГБ RAM. **Чистая установка полного стека на новой Linux-машине с SIP-шлюзом
и Asterisk не проверялась.**

## Как устроено

```
SIP-шлюз хакатона ── SIP 5060/udp, RTP 10000–20000 ──► Asterisk на хосте (systemd)
  10.250.0.6                                              │ ARI http://172.18.0.1:8090
                                                          │ externalMedia: RTP ulaw
                                                          ▼
                               голосовой шлюз (контейнер voice, 127.0.0.1:8001)
                               VAD → ASR → диалог → TTS, исходящие T11
                                                          │ сеть clinic-vm_default
                       ┌──────────────────────────────────┼─────────────────────┐
                       ▼                                  ▼                     ▼
             PostgreSQL (database)            backend (app, 0.0.0.0:8000)   data/ (индекс
             диалоги, бюджет МИС,             чат, API, панель /debug        расписания от
             журналы, задания T11                                            schedule-updater)
```

| Что | Где | Порт |
|---|---|---|
| backend, recovery, schedule-updater, PostgreSQL | `deploy/vm/compose.yaml`, проект `clinic-vm` | 8000 наружу, 5432 внутри сети |
| голосовой шлюз | `deploy/voice/compose.yaml`, проект `clinic-voice` | 8001 только на 127.0.0.1 |
| Asterisk 18 (пакет Ubuntu) | хост, `setup_asterisk.sh` | 5060/udp, RTP 10000–20000, ARI 8090 на адресе шлюза сети compose |

Порт 8088 на ВМ занят файловым менеджером платформы, поэтому ARI слушает 8090. Голос с
моделями занимает около 1 ГБ RAM, backend и остальные — по 50–80 МБ.

## Переменные

Общий `.env` в корне создаёт `deploy/vm/configure_env.py` (см. корневой README). Голосу из
него нужны: `APP_MODE`, `MIS_BASE_URL`, `MIS_TOKEN`, `MIS_RATE_LIMIT_PER_MIN`,
`MIS_TURN_WAIT_S`, `MIS_WAIT_LIMIT_S`, `CLINIC_UTC_OFFSET`, `POSTGRES_*`,
`DIALOGUE_ENABLED`, `DIALOGUE_MODE`, `DIALOGUE_STORAGE`, `DIALOGUE_HASH_KEY` (тот же, что у
backend: иначе панель и диалог не прочтут зашифрованные данные), `WEB_ACCESS_USER`,
`WEB_ACCESS_PASSWORD`, `SCHEDULE_INDEX_PATH`.

`deploy/voice/voice.env` — только голос (шаблон `voice.env.example`, права 600, не в Git и
не в образе):

| Переменная | Зачем |
|---|---|
| `ARI_USER`, `ARI_PASSWORD` | вход голоса в ARI; тот же пароль `setup_asterisk.sh` пишет в `ari.conf` |
| `DEBUG_PANEL_ENABLED` | события звонка в панели `/debug`; вместе с backend и только со входом команды |
| `REMINDERS_ENABLED` | исполнитель T11; включается **только здесь**, в backend — никогда |

`compose.yaml` задаёт остальное: `TELEPHONY_ENABLED=true`, адрес ARI и RTP Asterisk
(`COMPOSE_GATEWAY`, по умолчанию 172.18.0.1), формат `ulaw`, `VOICE_THREADS=2`,
`WEB_ACCESS_ENABLED=true`. `TELEPHONY_MEDIA_HOST` entrypoint вычисляет сам — это адрес
контейнера, на который Asterisk шлёт звук пациента.

## Модели

Каталог монтируется в `/models` только на чтение (`VOICE_MODELS_HOST`, по умолчанию
`models/` в корне). Сверка: `cd models && sha256sum -c ../deploy/voice/models.sha256`.

| Файл | Назначение | Источник |
|---|---|---|
| `gigaam/v3_ctc_onnx/v3_ctc_int8.onnx`, `v3_ctc.yaml` | ASR GigaAM v3 CTC, 8 бит | экспорт `bench/speech/export_gigaam.py` |
| `silero/silero_vad.onnx` | VAD Silero 5.1.2 | `bench/speech/download.py voice` |
| `silero/v5_5_ru.pt` | TTS Silero v5.5, голос xenia | `bench/speech/download.py voice` |

Экспорт и квантизация ASR идут в окружении замера (torch, gigaam), не в образе голоса.
Хеш int8-файла зависит от версии onnxruntime при квантизации; файл на ВМ — тот, что указан в
`models.sha256`. Если ваш экспорт дал другой хеш, это другая модель: сравните её на
`bench/speech` перед заменой и не меняйте модель одновременно с другими правками.

## Установка

Порядок важен: сеть `clinic-vm_default` и схема базы появляются на шаге 1.

```bash
# 1. backend, база, миграция, индекс расписания — по корневому README («Тестовый стенд»)
docker compose --env-file .env -f deploy/vm/compose.yaml up -d
# 2. модели в models/ и сверка
(cd models && sha256sum -c ../deploy/voice/models.sha256)
# 3. Asterisk и voice.env (пароль ARI генерируется один раз)
bash deploy/voice/setup_asterisk.sh
# 4. голос из того же коммита, что backend
SOURCE_COMMIT=$(git rev-parse HEAD) docker compose -f deploy/voice/compose.yaml up -d --build
# 5. проверка: всё «ок», итог 0
bash deploy/voice/check.sh
```

Голос считается готовым, когда `check.sh` прошёл и в журнале нет ошибок запуска:
`docker logs --since 5m clinic-voice-voice-1`. HTTP 200 от `/health/live` не проверяет SIP:
нужен звонок на номер команды.

**Исходящие T11** (после шага 5):

```bash
docker compose -f deploy/voice/compose.yaml exec voice \
  python -m app.reminders.cli contact-set --client-id <id тестового пациента>
# REMINDERS_ENABLED=true в deploy/voice/voice.env, затем пересоздать контейнер
docker compose -f deploy/voice/compose.yaml up -d
docker compose -f deploy/voice/compose.yaml exec voice python -m app.reminders.cli jobs
```

Звонить можно только адресам из `REMINDERS_ALLOWED_ENDPOINTS` (по умолчанию
`PJSIP/phone-021@gateway`). Окна и праздники — `config/calendar.yaml`; вне сверенного периода
обзвона нет.

## Замеры и наблюдение за звонком

- **Живой звонок:** `bash deploy/voice/monitor_call.sh 600 2 > звонок.log` запустить на ВМ до
  звонка. Скрипт только читает и раз в 2 с снимает CPU/RAM контейнеров, число каналов и
  RTP-статистику SIP-плеча (`pjsip show channelstats`). В конце он выписывает задержки по
  репликам из журнала голоса. Текстов и аудио в выводе нет. Хранить вне Git.
- **Без SIP, на целевых ресурсах:** запустить этот образ с `--cpus=2 --memory=4g`,
  `TELEPHONY_ENABLED=false`, `APP_MODE=offline_test`, `BACKEND_DEMO_ENABLED=true`,
  `DIALOGUE_MODE=natural`, затем выполнить
  `python bench/voice/load.py --sessions 2 --calls 4 --container <имя>`. Синтезированная речь
  чище человеческой, поэтому результат оптимистичнее живого звонка.

## Обновление, остановка, откат

Обновление — один согласованный выпуск для backend и голоса:

1. Убедиться, что звонков нет: `sudo asterisk -rx "core show channels"` → 0 active calls.
2. Сохранить базу (`pg_dump` из контейнера `database`) и частные настройки.
3. `git pull`, затем миграция новым образом backend (корневой README).
4. `SOURCE_COMMIT=$(git rev-parse HEAD) docker compose -f deploy/voice/compose.yaml up -d --build`.
5. `bash deploy/voice/check.sh`, затем контрольный звонок с открытой панелью.

Остановка голоса: `docker compose -f deploy/voice/compose.yaml down` — backend и база
продолжают работать. Asterisk: `sudo systemctl stop asterisk`. Откат конфигурации Asterisk к
пакетной: файлы лежат в `/etc/asterisk/.package-defaults`. Код старше текущей схемы на
обновлённой базе не запускать.

## Переход с `~/voice` на этот рецепт (текущая ВМ)

На ВМ голос сейчас собран из копии исходников `~/voice/src` (совпадает с `main` c537fb7) по
`~/voice/compose.yaml`, модели — в `~/voice/models`, пароль ARI — в `~/voice/.env`. Порты,
сеть, переменные и `ari.conf` совпадают с этим рецептом. Переход делает выпускающий:

1. `cp ~/voice/.env ~/project/deploy/voice/voice.env && chmod 600 ~/project/deploy/voice/voice.env`
2. В `~/project`: `VOICE_MODELS_HOST=$HOME/voice/models SOURCE_COMMIT=$(git rev-parse HEAD) docker compose -f deploy/voice/compose.yaml up -d --build`
   — имя проекта то же (`clinic-voice`), прежний контейнер заменяется.
3. `VOICE_MODELS_HOST=$HOME/voice/models bash deploy/voice/check.sh`

`setup_asterisk.sh` при переходе запускать не нужно: Asterisk уже настроен так же.

## Чего рецепт не делает

- **Перевод на регистратора не дозвонится.** Адресата `PJSIP/operator` на шлюзе нет. Перевод
  заканчивается фразой «звоните…», исход виден в панели как `voice.transfer_result`.
- **Автоответчик не распознаётся.** `AMD()` в dialplan не включён; VOICEMAIL проверен только
  контрактным тестом.
- **Второй голосовой процесс с тем же ARI-приложением `clinic` запускать нельзя.** Он
  перехватит звонки рабочего. Локальные проверки — с `TELEPHONY_ENABLED=false`.
