# T08 — результаты приёмки кандидата (19.10.2026)

Это матрица доказательств кандидата в локальной ветке `alex/t08-r01-review`, зафиксированного отдельным коммитом после проверок. Строгий сценарный отчёт был собран до коммита на том же коде приложения; последующие изменения затронули только тесты и отчёт. Кандидат не выпускался и не развёртывался. `covered` означает, что названная автоматическая проверка доказывает указанную часть; это не заменяет живую клиническую/SIP-приёмку. `partial` означает, что доказан только перечисленный подслучай. `open` означает, что доказательства не хватает.

Target T08 tests: `python ..\.development\run_t08_r01_tests.py`: 82 passed on isolated test PostgreSQL in 19.67 s. Full project run: 2351 passed, 27 skipped, 38 xfailed, 1 warning in 321.41 s. Ruff check/format (289 files) and `git diff --check` passed. Fake-ARI checks cover P2/P3 wait outcomes, card-before-dial, rejected bridges, callback consent and contact validation. Skipped/xfail are not counted as passes. Real SIP and the working VM were not tested.

Строгий `tests.scenarios.run --output-dir ..\.audit\t08-finish-t07-2026-10-19 --require all` завершился кодом 1: **19 pass / 22 fail / 16 unsupported**, `blocking_accepted=false`, `all_accepted=false`. Отчёт создан 19.10.2026 на HEAD `d43a1a004fcf6694c438c4c190ad01f5ef5b45be` с изменённым рабочим деревом; `source_sha256=be6693cc18ab3b700f8fb18aff571516cc3754d95d9102d6cc242d6872b2787d`. Эмулятор проверяет HTTP ASGI и локальный MIS; отдельно помечает SIP, ASR/TTS, внешнюю МИС и PostgreSQL restart/multiprocess как непроверенные. Не все эти проверки относятся к T08.

| № | Статус | Доказательство и граница |
|---|---|---|
| 01 | covered (fake ARI/CallSession) | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[True]` начинает с просьбы оператора сразу после приветствия; проверяет неизвестную личность, отсутствие `client_id`/MIS-операции и наличие карточки со статусом `attempting` до вызова fake ARI `originate`. GETdoctors в этом пути не вызывается. |
| 02 | partial | `tests/integration/test_pipeline.py::test_operator_request_at_held_booking_confirmation_never_confirms_booking` passed in Linux: operator request at booking confirmation returns R-02/P2, does not confirm the held booking, and creates no MIS POST. Strict `tests/scenarios/control/05_operator_any_step.yaml` remains unaccepted by the platform runner; release of the hold is not asserted by this HTTP check. |
| 03 | partial | `tests/integration/test_pipeline.py::test_operator_request_after_post_keeps_unknown_operation_without_retry` verifies an ordinary operator request racing a pending POST: R-02 is returned, the action id is retained, `committing` transitions to `reconciling` after the lost response, and no second POST is sent. A durable operator card and SIP transfer are not proven by this HTTP test. |
| 04 | partial | `tests/voice/test_telephony.py::test_verified_call_transfer_delivers_mis_escalation` runs an actual fake-ARI CallSession with a seeded verified identity, observes operator answer/bridge, then delivers one marker-bearing MIS escalation for that client. Linux passed. The identity is seeded in the dialogue store and MIS is a recorder; live identity lookup, production MIS, and SIP remain unverified. |
| 05 | covered (fake ARI / CallSession) | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[True-False-False]` begins with an anonymous caller requesting an operator, creates the P2 handoff before dialing, and makes no identity or MIS lookup. The Linux full T08 regression run passed. |
| 06 | covered (fake ARI / CallSession) | `tests/voice/test_telephony.py::test_full_p1_call_session_and_mis_429_never_create_callback` passed for no technical MIS client and configured MIS returning 429. The full safety-to-call path speaks the emergency response, attempts R-01/P1 with a 20-second timeout, records no-answer/manual handling, and creates no callback. Fake MIS only; no external MIS or SIP. |
| 07 | covered (fake ARI / CallSession) | `test_full_p1_call_session_and_mis_429_never_create_callback[None]` confirms unknown identity with no technical client, no MIS operation or call, and no callback; the guarded client makes an unexpected MIS escalation fail. |
| 08 | covered (fake ARI / CallSession) | `test_full_p1_call_session_and_mis_429_never_create_callback[None]` and `[999]` cover P1 no-answer, the 20-second originate timeout, manual result, and no callback for absent client and MIS 429. Both completed in Linux. |
| 09 | covered (fake ARI/CallSession) | `test_real_call_session_handles_fake_ari_no_answer[True]` exercises the P2 route, checks originate timeout 30 and no caller hangup before no-answer, then verifies the callback offer. No live SIP timing was measured. |
| 10 | covered (fake ARI/telephony) | `test_p3_operator_wait_repeats_status_without_ending_caller` checks repeated status prompts, no caller hangup while ringing, and prompt cancellation on answer; the same test checks originate timeout 90. Real SIP timing was not measured. |
| 11 | covered (fake ARI/HandoffService) | `tests/voice/test_telephony.py::test_answered_operator_with_rejected_bridge_is_reported_failed` отклоняет добавление ответившего оператора в мост; сохраняется `failed`, `connected` не фиксируется, MIS-операция не создаётся. |
| 12 | covered (fake ARI / CallSession) | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[True-True]` hangs up during callback offer after operator no-answer, closes the session, and verifies no callback draft or task remains. Linux regression run passed. |
| 13 | covered (service/store; Linux) | `tests/test_handoffs.py::test_conflicting_transfer_result_does_not_overwrite_attempt_history` sends a late conflicting `connected` result after a saved `no_answer` and verifies the original result, reason, and single attempt remain intact; targeted Linux run passed. |
| 14 | covered (test-only) | `tests/test_t08_postgres_review.py::test_two_os_processes_send_only_one_escalation` прошёл в целевом контуре: два spawned процесса используют отдельные соединения к одной тестовой PostgreSQL схеме и ровно один вызывает fake MIS POST. Реальная МИС не участвовала. |
| 15 | partial (external MIS) | Fake MIS verifies HTTP 201 parsing and rejects mismatched client/description without retry. `.env.example` points to the `crm-med-mock.dbi-testing.ru` test endpoint, but `MIS_TOKEN` is blank and no local credential/configuration for an approved contour is present. No external request was sent; real MIS remains unverified. |
| 16 | covered (isolated PostgreSQL + fake MIS) | `tests/integration/test_recovery.py::test_lost_post_is_found_after_new_worker_without_second_post[postgres]` passed: a fresh worker/store reads the durable operation, reconciles the emulator result, and leaves the single POST count unchanged. External MIS remains unverified. |
| 17 | covered (isolated PostgreSQL + reconstructed worker) | `tests/test_t08_postgres_review.py::test_restarted_worker_reconciles_sending_without_post` passed with persisted `sending` state and a new `HandoffService`; recovery performs one MIS read and no POST. It reconstructs the worker boundary rather than killing an OS process. |
| 18 | covered (fake MIS) | `test_uncertain_mis_reconciliation_never_applies_foreign_or_duplicate_marker[duplicate_marker]` leaves two matches for manual review instead of selecting the first. Real MIS remains unverified. |
| 19 | covered (fake MIS) | `test_reconciliation_marks_paginated_result_unstable_when_new_item_shifts_pages` inserts a record between pages; repeated IDs make the bounded scan manual, with no applied state or escalation ID. |
| 20 | covered (payload model) | `test_escalation_description_boundary_preserves_unicode_and_marker` checks Unicode at 1000 chars and rejection at 1001 or empty input. Real MIS delivery remains unverified. |
| 21 | covered (локальный сценарий) | `tests/voice/test_telephony.py::test_real_call_session_resumes_callback_after_fake_ari_no_answer`: CallSession + DialogueService + HandoffService, no_answer, согласие, контакт, срок, итоговое согласие, одна локальная задача. ARI fake, без SIP. |
| 22 | covered (service/store; Linux) | Existing tests verify decline and three unrecognized answers; `tests/test_handoffs_r01.py::test_ambiguous_callback_time_repeats_deadline_without_queuing` verifies a time question repeats the callback deadline, leaves the flow at time selection, and creates no task; targeted Linux run passed. |
| 23 | covered (memory + PostgreSQL) | `tests/test_t08_review.py::test_new_final_text_revokes_pending_confirmation_without_skipping_turn`, `tests/test_t08_postgres_review.py::test_postgres_new_final_text_revokes_callback_consent_across_store_instances` и `docs/agent/review/test_t08_remaining.py::test_new_final_text_revokes_pending_confirmation_without_skipping_turn`. Сохраняются HTTP `turn_id`, идемпотентность и CAS/input_epoch. |
| 24 | covered (HTTP/API + PostgreSQL) | `tests/test_t08_postgres_review.py::test_postgres_http_replay_of_final_callback_turn_returns_one_task` replays identical final consent POSTs through ASGI backed by PostgreSQL and verifies the same response and exactly one durable callback task. |
| 25 | covered (fake ARI/CallSession) | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[True]` проверяет маску и тестовое расшифрование контакта, продиктованного пациентом; идентификатор телефонного канала не используется как номер. |
| 26 | partial | `test_callback_calendar_matches_all_configured_2026_nonworking_days` checks every date of 2026 against the configured Saturday/weekend/holiday schedule; `test_callback_deadline_clinic_open_and_close_boundaries` checks 07:00/20:00 weekday and 08:30/15:00 Saturday boundaries in GMT+4. Both passed in Linux. The national transferred days match [Government Resolution No. 1466 of 24 September 2025](https://static.government.ru/media/files/4jeB8hNKm69ggOa9yDiYOli6YoAyM21i.pdf); clinic-specific exceptions and an approved clinic roster remain unverified. |
| 27 | covered (callback service) | `tests/test_handoffs_r01.py::test_preferred_callback_time_after_sla_requires_explicit_confirmation` проверяет предпочтение позже срока, предупреждение и сохранение выбора только после отдельного финального «да». |
| 28 | covered (PostgreSQL + operator HTTP API) | Two-store concurrent claim remains covered; `test_enabled_operator_routes_require_team_login` also submits a stale callback task version through the authenticated HTTP route and verifies 409 with task state unchanged. Linux PostgreSQL run passed. |
| 29 | covered (store/service) | `tests/test_handoffs_r01.py::test_callback_exhausts_after_three_attempts_and_keeps_sms_pending` прошёл: три no-answer исчерпывают очередь, claim номер четыре запрещён, `sms_pending=true` не объявляется доставленным. Реальной SMS нет. |
| 30 | covered (PostgreSQL recovery) | `tests/test_t08_postgres_review.py::test_expired_in_progress_callback_after_restart_becomes_manual_unknown` recovers a persisted in-progress operator call after lease expiry as `UNKNOWN`/`MANUAL`, marks the attempt `PROCESS_LOST`, and rejects a new claim. No repeat call is authorized. |
| 31 | covered (service/store) | `test_operator_finish_is_idempotent` verifies one stored attempt with `source=operator`, result `connected`, and idempotent repeat. External operator UI and telephony remain unverified. |
| 32 | partial | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[False]` прошёл: при выключенном callback after no-answer звучит `transfer.no_answer`, callback draft/task отсутствуют. Недоступность/отказ уже включённой операторской очереди в реальном запуске не проверялась. |
| 33 | partial | `test_enabled_operator_routes_require_team_login` now verifies unauthenticated requests return 401, cross-origin and duplicate-Origin requests return 403, same-origin authenticated access succeeds, and the operator page renders user-controlled content through `textContent`/`createTextNode` without `innerHTML`. Linux/PostgreSQL passed. Role-based permissions remain outside the implemented access model. |
| 34 | partial | `test_migrate_schema_nine_to_ten_preserves_existing_booking` прошёл: схема 9 мигрирована повторно, booking/session/receipt/reminder job/attempt/contact остались равными исходным данным, старая dialogue receipt читается через store. Другие журналы/квитанции/напоминания не представлены полным снимком рабочей БД. |
| 35 | covered (isolated PostgreSQL; T11 + RecoveryWorker) | `tests/integration/test_recovery.py::test_reminder_executor_and_recovery_worker_share_postgres_budget` runs the actual `ReminderExecutor.tick` and `RecoveryWorker.run_once` through separate `HttpMedicalSystem`/`PostgresBudget` instances sharing one PostgreSQL scope at 1 request/second. It verifies both accepted GETs are at least one second apart and the MIS emulator returns no 429; this is not production-load evidence. |
| 36 | covered (fake ARI / CallSession) | `tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer[True-False-True]` injects an emergency phrase during callback intake, verifies immediate P1/R-01 and the 20-second route, and confirms the callback draft/task are removed. No live SIP. |
| 37 | covered (diff) | `git diff --name-only` проверен: изменены dialogue/handoff/storage, их тесты и review fixture; scheduling, runner и RTP в изменениях отсутствуют. Это подтверждает границы diff, не функциональную приёмку. |

## Решение о выпуске

T08 остаётся **не принятой**: несколько пунктов имеют только частичное покрытие, реальные SIP/оператор и внешняя МИС не проверены. Целевой и полный pytest зелёные, но строгий текстовый прогон остаётся `19 pass / 22 fail / 16 unsupported`; его `unsupported` пути нельзя закрывать синтетическим `connected`. Кандидат не переносить в main до закрытия автоматизируемых пробелов и получения согласованных внешних SIP-доказательств; итоговые проверки повторить на чистом SHA.

## Краткая диагностика T07

Отчёт `../.audit/t08-final-t07-2026-10-19/results.json` разделяет наблюдаемый `fail` и непроверяемый `unsupported`. Для T08/T04 transfer+operator-card пункты (D-01, D-02, D-05, D-12, D-13, D-16, M-09 ДМС, R-07, R-09/11, R-OTHER) HTTP сценарий не доказывает установленное SIP соединение/полную операторскую карточку; их нельзя «исправить» синтетическим `connected`. Callback сценарии D-05/D-13/D-16 отмечены `unsupported`, где runner не подключает требуемые UI/audio prompt требования T08. В D-13 рабочий dialogue остаётся `RECONCILING` после неизвестного POST и runner не видит transfer request; это требует отдельного решения требованиями, нельзя слепо переводить неизвестную запись в эскалацию. Остальные fail относятся преимущественно к T09 (referral/home visit/FAQ/waitlist), T03 (silence/intermediate wait), T10 (identity/guardian), T13 (recording) и T20 (SMS). T07 отчёт оставлен без изменения; его `19/22/16` не трактуется как полная регрессия или pass только по совпадению со старым агрегированным числом.


## Linux follow-up (2026-10-19)

A read-only source snapshot of candidate `alex/t08-r01-review` at HEAD `eb1245d`, including the uncommitted regression test `tests/test_handoffs_r01.py::test_p1_without_technical_client_stays_manual_without_mis_lookup`, was run in a disposable Linux container. The container used Python 3.11.17 and the locked project development dependencies. No live MIS or SIP service was used.

Environment preparation command:

```sh
uv sync --project /tmp/t08-source --group dev --no-install-project --locked
```

Exact pytest command:

```sh
timeout --signal=TERM 90s /tmp/t08-test-venv/bin/python -m pytest tests/test_handoffs_r01.py::test_p1_without_technical_client_stays_manual_without_mis_lookup -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: **completed**, exit code **0**, `1 passed in 0.41s`. The 15-second faulthandler watchdog and 90-second process timeout did not fire. This proves the service-level P1 behavior without a configured technical client: the request is left for manual handling, no MIS operation is created, MIS is not called, and callback is not offered. It does not exercise the full P1 `CallSession` path; matrix items 07/08 remain partial until that coverage is run and reviewed.


## Shared T11 / RecoveryWorker budget follow-up (2026-10-19)

The combined test ran against a disposable PostgreSQL 17 container on the Linux VM using a source snapshot at HEAD `eb1245d` plus the current uncommitted regression changes. The test database, Docker network, containers, and source snapshot were removed after execution.

Environment preparation command:

```sh
uv sync --project /tmp/t08-source --group dev --no-install-project --locked
```

Exact pytest command:

```sh
timeout --signal=TERM 90s /tmp/t08-test-venv/bin/python -m pytest tests/integration/test_recovery.py::test_reminder_executor_and_recovery_worker_share_postgres_budget -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: **completed**, exit code **0**, `1 passed in 2.53s`. The test confirms sequential T11 reminder planning and RecoveryWorker reconciliation share one PostgreSQL request window across separate budget/client instances. The first deliberately mismatched fake/database clock attempt produced `200, 429, 200`; aligning the emulator and budget to the same advancing test clock removed the false 429, and the final test confirms zero limited requests. The 15-second faulthandler watchdog and 90-second timeout did not fire.


## Full P1 CallSession follow-up (2026-10-19)

The complete fake-ARI CallSession scenario ran in a disposable Linux container using the candidate source snapshot at HEAD `eb1245d` plus the current uncommitted regression changes. It exercises the emergency safety phrase, R-01/P1 transfer attempt, operator no-answer, manual handoff result, and the absence of a technical MIS client and callback.

Environment preparation command:

```sh
uv sync --project /tmp/t08-source --group dev --no-install-project --locked
```

Exact pytest command:

```sh
timeout --signal=TERM 90s /tmp/t08-test-venv/bin/python -m pytest tests/voice/test_telephony.py::test_full_p1_call_session_without_technical_client_stays_manual -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: **completed**, exit code **0**, `1 passed in 0.92s`. The faulthandler watchdog and process timeout did not fire. This is fake-ARI evidence; it does not verify real SIP.


## Linux full P1 and PostgreSQL recovery follow-up (2026-10-19)

The candidate source snapshot at HEAD `eb1245d`, including the uncommitted regression tests, ran in the prepared `clinic-t06-tests:python` Linux image (Python 3.11.17, pytest 9.1.1). P1 uses fake ARI and fake MIS; recovery uses a disposable PostgreSQL 17 container and a local MIS emulator. Docker network, database container, and uploaded source archive were removed after the run. An earlier attempt to bootstrap dependencies with `uv sync` stalled while downloading packages before pytest started; using the prepared test image allowed both test commands below to execute.

Exact P1 command:

```sh
PYTHONPATH=/tmp/t08-source timeout --signal=TERM 90s python -m pytest tests/voice/test_telephony.py::test_full_p1_call_session_and_mis_429_never_create_callback tests/voice/test_telephony.py::test_real_call_session_handles_fake_ari_no_answer -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: completed, exit code 0, **5 passed in 4.09s**. The two absent-client/MIS-429 cases passed, as did the no-answer callback-on/off paths and caller hangup during the callback offer. Hangup left no callback draft or task. The faulthandler watchdog and 90-second timeout did not fire.

Exact PostgreSQL recovery command:

```sh
timeout --signal=TERM 90s python -m pytest tests/test_t08_postgres_review.py::test_restarted_worker_reconciles_sending_without_post tests/test_t08_postgres_review.py::test_postgres_http_replay_of_final_callback_turn_returns_one_task tests/test_t08_postgres_review.py::test_enabled_operator_routes_require_team_login 'tests/integration/test_recovery.py::test_lost_post_is_found_after_new_worker_without_second_post[postgres]' 'tests/integration/test_recovery.py::test_crash_after_post_before_local_finish_is_recovered[postgres]' -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

The command ran in the test container with `PYTHONPATH=/tmp/t08-source` and `TEST_DATABASE_URL=postgresql+asyncpg://t08_test:t08_isolated_only@clinic-t08-review-pg:5432/t08_test`. Result: completed, exit code 0, **4 passed in 2.39s**. The 15-second faulthandler watchdog and 90-second timeout did not fire. PostgreSQL persistence, fresh worker reconstruction, and replay of the final callback HTTP request are covered; no external MIS or OS-level process kill was exercised.


## Linux targeted handoff follow-up (2026-10-19)

Exact command:

```sh
PYTHONPATH=/tmp/t08-source timeout --signal=TERM 90s python -m pytest tests/test_handoffs.py::test_conflicting_transfer_result_does_not_overwrite_attempt_history tests/test_handoffs_r01.py::test_ambiguous_callback_time_repeats_deadline_without_queuing -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: completed, exit code 0, **2 passed in 0.48s**. Both the conflicting transfer result and ambiguous callback-time question checks passed in Linux; neither watchdog nor process timeout fired.


## Linux callback HTTP replay follow-up (2026-10-19)

Exact command:

```sh
PYTHONPATH=/tmp/t08-source timeout --signal=TERM 90s python -m pytest tests/test_handoffs_r01.py::test_http_replay_of_final_callback_turn_returns_the_same_task -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: completed, exit code 0, **1 passed in 0.64s**. Repeating the same HTTP turn returned the original `CALLBACK_QUEUED` response and left exactly one task. Watchdog and process timeout did not fire.


## Linux targeted T08 regression suite (2026-10-19)

The complete targeted handoff, callback, telephony, and recovery module set ran against the prepared Linux test image and a disposable PostgreSQL 17 database. The full PostgreSQL fixture creates and drops isolated schemas. Test source was the candidate snapshot at HEAD `eb1245d` plus current uncommitted regression changes. The test container, database network/container, and uploaded source archive were removed after execution.

Exact pytest command:

```sh
timeout --signal=TERM 90s python -m pytest tests/test_handoffs.py tests/test_handoffs_r01.py tests/test_t08_review.py tests/test_t08_postgres_review.py tests/voice/test_telephony.py tests/integration/test_recovery.py -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

The command ran with `PYTHONPATH=/tmp/t08-source` and `TEST_DATABASE_URL=postgresql+asyncpg://t08_test:t08_isolated_only@clinic-t08-review-pg:5432/t08_test`. Result: completed, exit code 0, **130 passed in 26.46s**. No test was skipped in this selected set; the 15-second faulthandler watchdog and 90-second timeout did not fire. This is targeted T08 coverage, not the full repository suite or external SIP/MIS acceptance.


## Linux strict scenario acceptance (2026-10-19)

Exact command:

```sh
PYTHONPATH=/tmp/t08-source timeout --signal=TERM 90s python -m tests.scenarios.run --output-dir /tmp/t08-scenario-results --require blocking
```

Result: completed normally, exit code 1, **19 pass / 22 fail / 16 unsupported**; `blocking_accepted=false`, `all_accepted=false`. The 90-second process timeout did not fire. D-05 operator requests at greeting, during identification, and at booking confirmation are `unsupported`; D-05 outside operator hours is `fail`. The aggregate includes scenarios outside T08, as documented in the matrix analysis above. It does not establish T08 acceptance.


## Linux MIS HTTP 201 contract follow-up (2026-10-19)

Exact command:

```sh
PYTHONPATH=/tmp/t08-source timeout --signal=TERM 90s python -m pytest tests/test_handoffs_r01.py::test_escalation_http_201_requires_matching_client_and_description -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

Result: completed, exit code 0, **3 passed in 0.55s**. A valid HTTP 201 response is parsed; mismatched `client_id` or description is classified `MisOutcomeUnknown`, and each case performs exactly one POST. This uses HTTPX MockTransport, not a real MIS.

## Linux final T08 target regression (2026-10-19)

The targeted D-05 check and full target suite ran in the prepared `clinic-t06-tests:python` image (Linux, Python 3.11.17, pytest 9.1.1) with disposable PostgreSQL 17. Source came from the current checkout. No live SIP or external MIS was used. Docker network, PostgreSQL container, and source archive were removed after execution.

Exact pytest command (inside the test container):

```sh
timeout --signal=TERM 90s python -m pytest \
  tests/test_handoffs.py tests/test_handoffs_r01.py tests/test_t08_review.py \
  tests/test_t08_postgres_review.py tests/voice/test_telephony.py \
  tests/integration/test_recovery.py \
  tests/integration/test_pipeline.py::test_operator_request_at_held_booking_confirmation_never_confirms_booking \
  tests/integration/test_pipeline.py::test_operator_request_after_post_keeps_unknown_operation_without_retry \
  tests/voice/test_telephony.py::test_verified_call_transfer_delivers_mis_escalation \
  tests/test_handoffs_r01.py::test_callback_calendar_matches_all_configured_2026_nonworking_days \
  tests/test_handoffs_r01.py::test_callback_deadline_clinic_open_and_close_boundaries \
  -vv -s --tb=short -o faulthandler_timeout=15 -o cache_dir=/tmp/t08-pytest-cache --basetemp=/tmp/t08-pytest-tmp
```

With `PYTHONPATH=/tmp/t08-source` and `TEST_DATABASE_URL=postgresql+asyncpg://t08_test:t08_isolated_only@clinic-t08-review-pg:5432/t08_test`, the final command completed with exit code **0**: **135 passed in 26.79s**. This rerun includes the two D-05 checks, the verified-identity fake-ARI/MIS delivery check for item 04, full 2026 calendar/boundary checks for item 26, and operator Origin/content-safety checks for item 33. The 15-second faulthandler watchdog and 90-second process timeout did not fire.
