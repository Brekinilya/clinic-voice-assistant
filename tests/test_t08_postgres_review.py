"""Приёмка T08: обновление старой БД и независимые исполнители без настоящей МИС."""

import asyncio
import multiprocessing
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.dialogue.models import DialogueState, Phase, TransferRequest, Turn, reply
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.models import CallbackOutcome, CallbackState, MisDeliveryState
from app.handoffs.ports import HandoffConflict
from app.handoffs.service import HandoffService, HandoffSettings
from app.main import create_app
from app.mis.models import Escalation
from app.settings import Settings
from app.storage.booking import migrate
from app.storage.dialogue import PostgresDialogueStore
from app.storage.handoffs import PostgresHandoffStore
from app.storage.schema import (
    actions,
    dialogue_receipts,
    dialogue_sessions,
    metadata,
    reminder_attempts,
    reminder_contacts,
    reminder_jobs,
    schema_versions,
)
from tests.test_handoffs_r01 import Clock


@pytest.mark.asyncio
async def test_migrate_schema_nine_to_ten_preserves_existing_booking(pg_blank_engine):
    # Версия 9 отличается отсутствием шести добавочных таблиц T08.
    old_tables = [
        table
        for table in metadata.sorted_tables
        if not table.name.startswith(("backend_handoff_", "backend_callback_"))
    ]
    clock = Clock()
    state = DialogueState(call_id="legacy-dialogue").model_copy(
        update={"last_turn_id": 1, "version": 1}
    )
    old_reply = reply(state, 1, "Сохранённый ответ до миграции.")
    reminder_id = "b" * 32
    attempt_id = "c" * 32
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=old_tables))
        await connection.execute(insert(schema_versions).values(version=9))
        await connection.execute(
            insert(actions).values(
                action_id="a" * 32,
                call_id="old-call",
                revision=1,
                client_id=71,
                doctor_id=6,
                scheduled_at=clock.now() + timedelta(days=2),
                specialty="лор",
                expires_at=clock.now() + timedelta(minutes=2),
                state="booked",
                confirmed_turn=5,
                visit_id=257,
            )
        )
        await connection.execute(
            insert(dialogue_sessions).values(
                call_id=state.call_id,
                state=state.model_dump(mode="json"),
                pending_turn=None,
            )
        )
        await connection.execute(
            insert(dialogue_receipts).values(
                call_id=state.call_id,
                turn_id=1,
                digest="d" * 64,
                reply=old_reply.model_dump(mode="json"),
            )
        )
        await connection.execute(
            insert(reminder_jobs).values(
                id=reminder_id,
                visit_id=257,
                client_id=71,
                doctor_id=6,
                scheduled_at=clock.now() + timedelta(days=2),
                call_day=clock.now().date(),
                state="planned",
                next_at=clock.now(),
                attempts=0,
                outcome=None,
                mis_status="pending",
                reason=None,
                sms_pending=True,
                version=1,
            )
        )
        await connection.execute(
            insert(reminder_attempts).values(
                id=attempt_id,
                job_id=reminder_id,
                number=1,
                fence=1,
                owner="legacy-worker",
                lease_until=clock.now() + timedelta(minutes=1),
                state="unknown",
                channel_id="legacy-channel",
                result=None,
                mis_status="pending",
                reason="legacy_unknown",
                started_at=clock.now(),
                finished_at=None,
            )
        )
        await connection.execute(
            insert(reminder_contacts).values(client_id=71, endpoint="test-endpoint")
        )
        before = {
            table.name: (await connection.execute(select(table))).mappings().all()
            for table in (
                actions,
                dialogue_sessions,
                dialogue_receipts,
                reminder_jobs,
                reminder_attempts,
                reminder_contacts,
            )
        }
    # Повтор миграции также должен быть безопасен.
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [10]
        after = {
            table.name: (await connection.execute(select(table))).mappings().all()
            for table in (
                actions,
                dialogue_sessions,
                dialogue_receipts,
                reminder_jobs,
                reminder_attempts,
                reminder_contacts,
            )
        }
        assert after == before
    replay = await PostgresDialogueStore(pg_blank_engine).claim(state.call_id, 1, "d" * 64)
    assert replay.cached == old_reply
    assert await PostgresHandoffStore(pg_blank_engine).list_requests(limit=50, offset=0) == []


@pytest.mark.asyncio
async def test_postgres_callback_terminal_result_is_durable_and_idempotent(pg_engine):
    store = PostgresDialogueStore(pg_engine)
    call = "postgres-callback-terminal"
    handoff = await _preempt_after_turns(store, call)
    result = reply(handoff, 5, "Обратный звонок не сохранён.", code="CALLBACK_DECLINED").model_copy(
        update={"phase": Phase.ENDED}
    )

    state, saved = await store.finish_handoff_turn(
        call, 5, "callback-decline-digest", handoff.version, result
    )
    repeated = await store.claim(call, 5, "callback-decline-digest")

    assert state.phase == Phase.ENDED
    assert saved is not None and saved.phase == Phase.ENDED
    assert repeated.cached == saved
    assert (await store.inspect(call)).phase == Phase.ENDED


@pytest.mark.asyncio
async def test_postgres_http_replay_of_final_callback_turn_returns_one_task(pg_engine):
    clock = Clock()
    call = "postgres-callback-http-replay"
    dialogue_store = PostgresDialogueStore(pg_engine)
    handoff_store = PostgresHandoffStore(pg_engine)
    handoffs = HandoffService(
        handoff_store,
        object(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    state = await _preempt_after_turns(dialogue_store, call)
    event = SimpleNamespace(
        attempt_id=f"{call}-attempt", call_id=call, route="R-08", priority="P3"
    )
    await handoffs.transfer_started(event)
    await handoffs.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await handoffs.start_callback(call, 1, "no_answer")
    await handoffs.callback_turn(call, Turn(turn_id=2, text="да"))
    await handoffs.callback_turn(call, Turn(turn_id=3, text="79991234567"))
    await handoffs.callback_turn(call, Turn(turn_id=4, text="в ближайшее время"))
    dialogue = DialogueService(
        None,
        dialogue_store,
        b"x" * 32,
        safety=RulesSafety(clock),
        handoffs=handoffs,
    )

    @asynccontextmanager
    async def dialogue_factory(settings, selected_clock):
        yield dialogue

    app = create_app(
        Settings(_env_file=None, backend_demo_enabled=True),
        clock=clock,
        dialogue_factory=dialogue_factory,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            payload = {"turn_id": 5, "text": "да"}
            first = await client.post(f"/api/dialog/{call}/turn", json=payload)
            replay = await client.post(f"/api/dialog/{call}/turn", json=payload)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["code"] == "CALLBACK_QUEUED"
    tasks = await handoff_store.list_callback_tasks(limit=50, offset=0)
    saved_request = await handoff_store.request_for_call(call)
    assert len(tasks) == 1 and saved_request is not None
    assert tasks[0].request_id == saved_request.request_id
    assert await handoff_store.get_callback_task_by_request(tasks[0].request_id) == tasks[0]


@pytest.mark.asyncio
async def test_postgres_new_p1_fences_stale_callback_terminal_result(pg_engine):
    store = PostgresDialogueStore(pg_engine)
    call = "postgres-callback-p1-race"
    handoff = await _preempt_after_turns(store, call)
    await store.preempt(call, 6, TransferRequest(route="R-01", priority="P1"))
    result = reply(handoff, 5, "Обратный звонок не сохранён.", code="CALLBACK_DECLINED").model_copy(
        update={"phase": Phase.ENDED}
    )

    state, saved = await store.finish_handoff_turn(
        call, 5, "stale-callback-digest", handoff.version, result
    )

    assert state.phase == Phase.HANDOFF
    assert state.transfer is not None and state.transfer.priority == "P1"
    assert saved is None


@pytest.mark.asyncio
async def test_postgres_new_final_text_revokes_callback_consent_across_store_instances(
    pg_engine, monkeypatch
):
    """A newer final utterance fences a delayed consent commit in PostgreSQL."""
    clock = Clock()
    settings = HandoffSettings(enabled=True, callback_enabled=True)
    handoff_store_a = PostgresHandoffStore(pg_engine)
    handoff_store_b = PostgresHandoffStore(pg_engine)
    handoffs_a = HandoffService(handoff_store_a, object(), clock, b"x" * 32, settings)
    handoffs_b = HandoffService(handoff_store_b, object(), clock, b"x" * 32, settings)
    dialogue_store_a = PostgresDialogueStore(pg_engine)
    dialogue_store_b = PostgresDialogueStore(pg_engine)
    dialogue_a = DialogueService(
        None, dialogue_store_a, b"x" * 32, safety=RulesSafety(clock), handoffs=handoffs_a
    )
    dialogue_b = DialogueService(
        None, dialogue_store_b, b"x" * 32, safety=RulesSafety(clock), handoffs=handoffs_b
    )
    call = "postgres-final-text-revokes-callback"

    for turn_id in range(1, 5):
        claim = await dialogue_store_a.claim(call, turn_id, f"seed-{turn_id}")
        if turn_id == 4:
            await dialogue_store_a.preempt(
                call, turn_id, TransferRequest(route="R-08", priority="P3")
            )
        else:
            await dialogue_store_a.complete(
                claim.state, turn_id, reply(claim.state, turn_id, "Продолжаем.")
            )

    event = SimpleNamespace(attempt_id=f"{call}-attempt", call_id=call, route="R-08", priority="P3")
    await handoffs_a.transfer_started(event)
    await handoffs_a.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await handoffs_a.start_callback(call, 1, "no_answer")
    await handoffs_a.callback_turn(call, Turn(turn_id=2, text="да"))
    await handoffs_a.callback_turn(call, Turn(turn_id=3, text="79991234567"))
    await handoffs_a.callback_turn(call, Turn(turn_id=4, text="в ближайшее время"))

    entered, release = asyncio.Event(), asyncio.Event()
    original = handoff_store_a.create_callback_task

    async def delayed(task, **authorization):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(task, **authorization)

    monkeypatch.setattr(handoff_store_a, "create_callback_task", delayed)
    writer = asyncio.create_task(dialogue_a.handle(call, Turn(turn_id=5, text="да")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        newer = await dialogue_b.handle(call, Turn(turn_id=6, text="79990000000"))
        assert newer.code != "CALLBACK_QUEUED"
    finally:
        release.set()
        await asyncio.wait_for(writer, 5)

    request = await handoff_store_b.request_for_call(call)
    assert request is not None
    assert await handoff_store_b.get_callback_task_by_request(request.request_id) is None
    draft = await handoff_store_b.get_callback_draft(request.request_id)
    assert draft is not None
    assert draft.last_turn_id == 6
    assert draft.observed_turn_id == 6


async def _preempt_after_turns(store, call, last_turn_id=4):
    for turn_id in range(1, last_turn_id + 1):
        claim = await store.claim(call, turn_id, f"seed-{turn_id}")
        if turn_id == last_turn_id:
            return await store.preempt(call, turn_id, TransferRequest(route="R-08", priority="P3"))
        await store.complete(claim.state, turn_id, reply(claim.state, turn_id, "Продолжаем."))


class MisSpy:
    def __init__(self):
        self.posts = 0
        self.reads = 0

    async def create_escalation(self, payload):
        self.posts += 1
        await asyncio.sleep(0.02)
        return Escalation(
            id=42,
            client_id=payload.client_id,
            description=payload.description,
            created_at="19.10.2026 10:00",
        )

    async def escalations(self, client_id, *, limit, offset):
        self.reads += 1
        return []


async def connected_request(engine, mis):
    service = HandoffService(
        PostgresHandoffStore(engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    event = SimpleNamespace(
        attempt_id="verified-attempt", call_id="verified-call", route="R-08", priority="P3"
    )
    await service.transfer_started(event, DialogueState(call_id=event.call_id, client_id=71))
    await service.transfer_result(SimpleNamespace(**vars(event), outcome="connected", reason=None))
    request = await service.store.request_for_call(event.call_id)
    assert request.mis_operation_id is not None
    return service, request


@pytest.mark.asyncio
async def test_two_postgres_store_instances_send_only_one_escalation(pg_engine):
    mis = MisSpy()
    first, request = await connected_request(pg_engine, mis)
    second = HandoffService(
        PostgresHandoffStore(pg_engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    await asyncio.gather(first.deliver_mis_once(), second.deliver_mis_once())
    assert mis.posts == 1
    result = await first.store.get_mis_operation(request.mis_operation_id)
    assert result.state == MisDeliveryState.APPLIED


class ProcessMisSpy:
    def __init__(self, posts, lock):
        self.posts = posts
        self.lock = lock

    async def create_escalation(self, payload):
        with self.lock:
            self.posts.value += 1
        await asyncio.sleep(0.1)
        return Escalation(
            id=42,
            client_id=payload.client_id,
            description=payload.description,
            created_at="19.10.2026 10:00",
        )

    async def escalations(self, client_id, *, limit, offset):
        return []


def _deliver_from_process(schema, posts, lock, barrier):
    async def run():
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise RuntimeError("TEST_DATABASE_URL is required for this test-only worker")
        engine = create_async_engine(
            url,
            pool_size=1,
            max_overflow=0,
            hide_parameters=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            service = HandoffService(
                PostgresHandoffStore(engine),
                ProcessMisSpy(posts, lock),
                Clock(),
                b"x" * 32,
                HandoffSettings(enabled=True),
            )
            barrier.wait(timeout=10)
            await service.deliver_mis_once()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_two_os_processes_send_only_one_escalation(pg_engine):
    """Two independently started workers contend on the same test PostgreSQL row."""
    mis = MisSpy()
    first, request = await connected_request(pg_engine, mis)
    async with pg_engine.connect() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()

    context = multiprocessing.get_context("spawn")
    posts = context.Value("i", 0)
    lock = context.Lock()
    barrier = context.Barrier(2)
    workers = [
        context.Process(target=_deliver_from_process, args=(schema, posts, lock, barrier))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    try:
        assert all(not worker.is_alive() for worker in workers), "worker process timed out"
        assert [worker.exitcode for worker in workers] == [0, 0]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)

    assert posts.value == 1
    operation = await first.store.get_mis_operation(request.mis_operation_id)
    assert operation.state == MisDeliveryState.APPLIED


@pytest.mark.asyncio
async def test_two_postgres_operators_cannot_claim_the_same_callback(pg_engine):
    clock = Clock()
    first_store, second_store = PostgresHandoffStore(pg_engine), PostgresHandoffStore(pg_engine)
    settings = HandoffSettings(enabled=True, callback_enabled=True)
    first = HandoffService(first_store, MisSpy(), clock, b"x" * 32, settings)
    second = HandoffService(second_store, MisSpy(), clock, b"x" * 32, settings)
    event = SimpleNamespace(
        attempt_id="operator-claim-race-attempt",
        call_id="operator-claim-race-call",
        route="R-08",
        priority="P3",
    )
    await first.transfer_started(event)
    await first.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await first.start_callback(event.call_id, 1, "no_answer")
    await first.callback_turn(event.call_id, Turn(turn_id=2, text="да"))
    await first.callback_turn(event.call_id, Turn(turn_id=3, text="79991234567"))
    await first.callback_turn(event.call_id, Turn(turn_id=4, text="в ближайшее время"))
    await first.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
    request = await first_store.request_for_call(event.call_id)
    task = await first_store.get_callback_task_by_request(request.request_id)

    outcomes = await asyncio.gather(
        first.operator_claim(task.task_id, "operator-a"),
        second.operator_claim(task.task_id, "operator-b"),
        return_exceptions=True,
    )
    claimed = [item for item in outcomes if not isinstance(item, Exception)]
    conflicts = [item for item in outcomes if isinstance(item, HandoffConflict)]
    assert len(claimed) == len(conflicts) == 1
    saved = await second_store.get_callback_task(task.task_id)
    assert saved.state.value == "claimed"
    assert saved.owner in {"operator-a", "operator-b"}


@pytest.mark.asyncio
async def test_expired_in_progress_callback_after_restart_becomes_manual_unknown(pg_engine):
    clock = Clock()
    store = PostgresHandoffStore(pg_engine)
    service = HandoffService(
        store,
        MisSpy(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    event = SimpleNamespace(
        attempt_id="callback-process-lost-transfer",
        call_id="callback-process-lost-call",
        route="R-08",
        priority="P3",
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await service.start_callback(event.call_id, 1, "no_answer")
    await service.callback_turn(event.call_id, Turn(turn_id=2, text="да"))
    await service.callback_turn(event.call_id, Turn(turn_id=3, text="79991234567"))
    await service.callback_turn(event.call_id, Turn(turn_id=4, text="в ближайшее время"))
    await service.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
    request = await store.request_for_call(event.call_id)
    task = await store.get_callback_task_by_request(request.request_id)
    claimed = await service.operator_claim(task.task_id, "operator-a")
    in_progress, attempt = await service.operator_start_attempt(
        task.task_id, "operator-a", claimed.version
    )
    assert in_progress.state == CallbackState.IN_PROGRESS and in_progress.attempts == 1

    clock.value += timedelta(minutes=11)
    restarted = HandoffService(
        PostgresHandoffStore(pg_engine),
        MisSpy(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    recovered = await restarted.recover_callbacks_once()

    saved = await restarted.store.get_callback_task(task.task_id)
    history = await restarted.callback_attempt_history(task.task_id)
    assert len(recovered) == 1 and saved is not None
    assert saved.state == CallbackState.MANUAL
    assert saved.last_outcome == CallbackOutcome.UNKNOWN
    assert saved.attempts == 1 and saved.active_attempt_id is None
    assert len(history) == 1 and history[0].attempt_id == attempt.attempt_id
    assert history[0].outcome == CallbackOutcome.UNKNOWN
    assert history[0].reason == "PROCESS_LOST" and history[0].finished_at is not None
    with pytest.raises(HandoffConflict):
        await restarted.operator_claim(task.task_id, "operator-b")


@pytest.mark.asyncio
async def test_restarted_worker_reconciles_sending_without_post(pg_engine):
    mis = MisSpy()
    first, request = await connected_request(pg_engine, mis)
    operation = await first.store.get_mis_operation(request.mis_operation_id)
    await first.store.update_mis_operation(
        operation.model_copy(
            update={
                "state": MisDeliveryState.SENDING,
                "sending_at": Clock().now(),
                "next_check_at": Clock().now(),
            }
        ),
        expected_version=operation.version,
    )
    restarted = HandoffService(
        PostgresHandoffStore(pg_engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    await restarted.deliver_mis_once()
    assert mis.posts == 0 and mis.reads == 1
    checked = await restarted.store.get_mis_operation(operation.operation_id)
    assert checked.state == MisDeliveryState.UNKNOWN


@pytest.mark.asyncio
async def test_enabled_operator_routes_require_team_login(pg_engine, tmp_path):
    """Настоящая фабрика HTTP-приложения, изолированный журнал, подмена только МИС."""
    service, _ = await connected_request(pg_engine, MisSpy())
    password = "fictional-operator-review-password"
    settings = Settings(
        _env_file=None,
        app_mode="hackathon",
        mis_token="fictional-token",
        dialogue_enabled=True,
        dialogue_mode="natural",
        dialogue_storage="postgres",
        schedule_index_path=tmp_path / "fixture.sqlite",
        dialogue_hash_key="x" * 32,
        web_access_enabled=True,
        web_access_password=password,
        handoffs_enabled=True,
        callback_enabled=True,
    )

    class Database:
        async def ping(self):
            pass

        async def aclose(self):
            pass

    @asynccontextmanager
    async def dialogue_factory(settings, clock):
        yield SimpleNamespace(handoffs=service)

    app = create_app(
        settings, database_factory=lambda _: Database(), dialogue_factory=dialogue_factory
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for path in ("/operator", "/api/operator/requests", "/api/operator/callbacks"):
                denied = await client.get(path)
                assert denied.status_code == 401
                allowed = await client.get(path, auth=("clinic", password))
                assert allowed.status_code == 200
            response = await client.get("/api/operator/requests", auth=("clinic", password))
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["items"][0]["transfer_state"] == "connected"

            cross_origin = await client.get(
                "/api/operator/requests",
                auth=("clinic", password),
                headers={"origin": "https://attacker.invalid"},
            )
            assert cross_origin.status_code == 403
            duplicate_origin = await client.get(
                "/api/operator/requests",
                auth=("clinic", password),
                headers=[("origin", "http://testserver"), ("origin", "https://attacker.invalid")],
            )
            assert duplicate_origin.status_code == 403
            same_origin = await client.get(
                "/api/operator/requests",
                auth=("clinic", password),
                headers={"origin": "http://testserver"},
            )
            assert same_origin.status_code == 200

            injected = "<img src=x onerror=alert(1)>"
            created = await client.post(
                "/api/operator/requests",
                auth=("clinic", password),
                headers={"origin": "http://testserver"},
                json={"route": "R-08", "priority": "P3", "summary": injected},
            )
            assert created.status_code == 200
            page = await client.get(
                "/operator", auth=("clinic", password), headers={"origin": "http://testserver"}
            )
            assert page.status_code == 200
            assert "textContent" in page.text and "createTextNode" in page.text
            assert "innerHTML" not in page.text

            service.settings = HandoffSettings(enabled=True, callback_enabled=True)
            event = SimpleNamespace(
                attempt_id="operator-http-version-attempt",
                call_id="operator-http-version-call",
                route="R-08",
                priority="P3",
            )
            await service.transfer_started(event)
            await service.transfer_result(
                SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
            )
            await service.start_callback(event.call_id, 1, "no_answer")
            await service.callback_turn(event.call_id, Turn(turn_id=2, text="да"))
            await service.callback_turn(event.call_id, Turn(turn_id=3, text="79991234567"))
            await service.callback_turn(event.call_id, Turn(turn_id=4, text="в ближайшее время"))
            queued = await service.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
            assert queued.code == "CALLBACK_QUEUED"
            callback_request = await service.store.request_for_call(event.call_id)
            task = await service.store.get_callback_task_by_request(
                callback_request.request_id
            )
            claimed = await service.operator_claim(task.task_id, "operator-a")
            stale = await client.post(
                f"/api/operator/callbacks/{task.task_id}/attempts",
                json={"owner": "operator-a", "version": claimed.version - 1},
                auth=("clinic", password),
            )
            assert stale.status_code == 409
            current = await service.store.get_callback_task(task.task_id)
            assert current.version == claimed.version and current.state.value == "claimed"
