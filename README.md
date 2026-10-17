# Голосовой ассистент регистратуры

Принимает звонки пациентов поликлиники, записывает на приём, переносит и отменяет записи, напоминает о визитах. Медицинские, конфликтные и нестандартные обращения передаёт регистратору вместе с карточкой разговора.

Кейс DBI, продуктовый хакатон «Точка сборки», 13–31 октября 2026.

## Статус

Реализованы текстовый диалог, браузерный голосовой канал, SIP/ARI, правила NLU и safety,
импорт расписания Excel, запись, перенос и отмена после подтверждения, журналы PostgreSQL.
Текст и голос используют один диалоговый сервис. Это прототип: исходящие напоминания,
полноценная карточка оператора, callback, `/escalations` и часть FAQ требуют завершения.
Устойчивость телефонии дорабатывается в T04, бюджет МИС — в T05.

Проверенная основа T03 — `13bd7c7`: локально **1724 passed, 63 skipped**,
включая 56 пропущенных полных сценарных тестов. Это проверка с PostgreSQL,
а не доказательство работы выданной ВМ, внешней МИС или всех телефонных сценариев.
Результаты новых версий нужно подтверждать отдельно.

## Стек

- Python 3.11, FastAPI, httpx, Pydantic
- Голос: Silero VAD, GigaAM CTC ONNX, Silero TTS; очереди и ожидания ограничены
- Телефония: Asterisk SIP/ARI/RTP; также есть браузерный голосовой клиент
- PostgreSQL — диалоги и операции; SQLite — проверенное расписание
- Сейчас понимание выполняет RulesNlu; интеграция LLM-шлюза ещё не завершена

## Структура

```
app/
  api/              внутреннее API и панель
  dialogue/         состояния, нормализация, шаблоны ответов
  policies/         маршрутизация и ограничения
  domain/           пациенты, слоты, действия
  mis/              клиент стенда «Регистратура»
  voice/            VAD, распознавание, синтез, медиа
  outbox/           долговечная очередь заданий
  scheduling/       импорт Excel и поиск слотов
  booking/          запись, удержания, подтверждение и журнал
  cancellation/     отмена записи
  rescheduling/     перенос записи
  storage/          модели и миграции
config/             правила, маршруты, настройки окружений
fixtures/synthetic/ только вымышленные данные
tests/              диалоги, контракт стенда, гонки, аудио
deploy/             compose, asterisk
docs/
```

## Договорённости

- Токены, ключи и реальные персональные данные в репозиторий не попадают. Секреты — только в `.env`, шаблон в `.env.example`.
- Материалы кейса (регламент, выгрузки CSV) лежат локально в `data/` и не коммитятся.
- Режимы работы: `offline_test` (эмулятор стенда), `hackathon` (настоящий стенд, вымышленные данные), `clinic_pilot`. Незаметно подменять стенд эмулятором нельзя.
- Время стенда — GMT+4, в строках дат смещение не передаётся.
- Сдача — `main` выданного Gitea, все сервисы в одном репозитории. С 18.10 ежедневное
  LLM-ревью проходит с 06:00 до 10:00 МСК; push нужен до 06:00. GitHub его не заменяет.
  По актуальной [памятке](https://hub.gigaschool.ru/events/tochka-sborki-2026/guide)
  стоп-код — 29.10.2026 в 00:01 МСК, финальная защита — 31.10.

## Запуск

### Текстовый демонстрационный режим

Нужны Python 3.11+, `uv` и Docker Compose. Из корня проекта на Linux:

```bash
uv sync --locked
uv run --no-sync python deploy/vm/configure_env.py --mode offline_test
mkdir -p data
docker compose --env-file .env -f deploy/vm/compose.yaml build app
docker compose --env-file .env -f deploy/vm/compose.yaml up -d database
docker compose --env-file .env -f deploy/vm/compose.yaml run --rm app python -m app.backend migrate
docker compose --env-file .env -f deploy/vm/compose.yaml up -d app
```

Скрипт создаёт `.env` с новыми паролем базы и постоянным ключом диалога;
существующий файл не перезаписывает. Не удаляйте рабочий `.env` для повторного запуска:
иначе будут потеряны постоянные реквизиты базы и диалога. Пароли не выводятся.
Локальный `compose.yaml` сохраняет привязку к localhost; отдельный `deploy/vm/compose.yaml`
публикует порт 8000 для прокси платформы, не открывая порт PostgreSQL.

Чат — `http://127.0.0.1:8000/chat` или публичный HTTPS-адрес выданной ВМ.
Учебный пациент — **Иванов Иван Иванович, 01.01.1990**.
Начните с «Хочу записаться к терапевту», затем ФИО с датой рождения, выбор времени и «Да».
Страница явно обозначает эмулятор с вымышленным расписанием.
Пациенты и записи эмулятора находятся в памяти: перезапуск их сбрасывает,
даже если квитанции диалога сохранились в PostgreSQL. После перезапуска начинайте новый диалог.

Без Docker установите PostgreSQL, настройте `.env.example`, включите
`BACKEND_DEMO_ENABLED=true`, `DIALOGUE_MODE=natural`, `DIALOGUE_STORAGE=postgres`, задайте
постоянный `DIALOGUE_HASH_KEY` от 32 байт, затем:

```bash
uv run --no-sync python -m app.backend migrate
uv run --no-sync uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

`/health/live` проверяет процесс, `/health/ready` — PostgreSQL.
Они не подтверждают доступность МИС, качество ASR или прохождение сценариев.

### Тестовый стенд организаторов и Excel

Платформа задаёт `DBI_API_TOKEN` и `DBI_API_URL`, приложение использует
`MIS_TOKEN` и `MIS_BASE_URL`. `configure_env.py --mode hackathon` сопоставляет их
и приводит адрес стенда к `/api/v1`. Для этого варианта создавайте `.env` **до первого
запуска**, без существующего рабочего файла. Для перехода с уже работающего эмулятора
сохраните пароль базы и постоянный ключ диалога, изменив только режим и параметры МИС.

```bash
uv run --no-sync python deploy/vm/configure_env.py --mode hackathon
mkdir -p data
docker compose --env-file .env -f deploy/vm/compose.yaml build app
docker compose --env-file .env -f deploy/vm/compose.yaml up -d database
docker compose --env-file .env -f deploy/vm/compose.yaml run --rm app python -m app.backend migrate
docker compose --env-file .env -f deploy/vm/compose.yaml run --rm --user "$(id -u):$(id -g)" app \
  python -m app.scheduling.cli fetch-doctors --output data/doctors.json
docker compose --env-file .env -f deploy/vm/compose.yaml run --rm --user "$(id -u):$(id -g)" app \
  python -m app.scheduling.cli import-xlsx data/schedule.xlsx \
  --catalog data/doctors.json --output data/schedule.sqlite
docker compose --env-file .env -f deploy/vm/compose.yaml run --rm app \
  python -m app.scheduling.cli check data/schedule.sqlite
docker compose --env-file .env -f deploy/vm/compose.yaml up -d app
```

Предварительно скопируйте выданную книгу в `data/schedule.xlsx`. ID Excel вроде `D001`
не являются ID МИС: импорт сопоставляет ФИО и профиль. Индекс привязан к режиму,
адресу и токену команды. По умолчанию срок актуальности — 24 часа; контролируемое
обновление выполняется повторным импортом с `--replace` после сверки справочника.
Неподтверждённые участок или направление могут потребовать регистратора:
отсутствие факта допуска не считается разрешением.

Внешний лимит — 10 запросов в минуту на команду, внутренний пока 8.
Импорт, запуск и другие процессы делят бюджет; запуск сразу после чтения каталога
может упереться в лимит. Управляемое ожидание и кэш дорабатываются в T05.
В `hackathon` чат работает с настоящим тестовым стендом на вымышленных пациентах;
незаметной подмены эмулятором нет. Не печатайте `.env`, `/opt/hackathon/ai.env`
или `docker compose config` в общий лог: развёрнутая конфигурация содержит секреты.

### Голос и телефон

Текстовый контейнер выше не устанавливает голосовые зависимости. На машине с моделями:

```bash
uv sync --locked --extra voice
uv run --no-sync python -m app.voice.preflight --warm-up
uv run --no-sync uvicorn app.voice.server:create_voice_app --factory --host 0.0.0.0 --port 8000
```

Нужны включённый диалог и веса в `models/`; экспорт и хеши описаны в
[`bench/speech/README.md`](bench/speech/README.md). Браузерный канал — `/web/voice.html`,
микрофон требует HTTPS либо localhost. Настройка SIP, RTP, паролей и учебных звонков —
[`deploy/asterisk/README.md`](deploy/asterisk/README.md).
Публичный HTTP-адрес сам по себе не открывает SIP/RTP на ВМ.
Тестовые процессы запускайте с `TELEPHONY_ENABLED=false`, чтобы не перехватить
основную ARI-регистрацию `clinic`.

### Проверки и ограничения

```bash
uv sync --locked --extra voice
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
APP_MODE=offline_test TELEPHONY_ENABLED=false uv run --no-sync pytest
```

Для интеграционных тестов нужен `TEST_DATABASE_URL` к отдельной PostgreSQL;
тесты создают и удаляют собственные схемы. Голосовые unit-тесты не скачивают модели.

LLM/NLU извлекает намерения и параметры; проверка пациента, времени, права на действие
и подтверждение находятся вне модели. Неопределённый ответ на POST требует сверки,
а новая речь отзывает прежнее согласие. Ассистент не ставит диагноз и не назначает лечение.
`clinic_pilot` отключён: допуск к настоящим данным и защищённый контур не подтверждены.

Конфигурация ВМ подготовлена для развёртывания; её успешный запуск на выданной машине
нужно подтвердить отдельно. Браузерный чат позволяет показать сценарий текстом,
если голос требует настройки, с явным указанием режима МИС.
