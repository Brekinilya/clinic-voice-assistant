"""Проверяем обработку финальных команд, повторов, задержек и окончания разговора."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from app.booking.memory import MemoryBookingStore
from app.booking.service import BookingService
from app.demo import DemoClock, DemoSchedule, EmulatorBudget, demo_emulator
from app.dialogue.input import Command, Operation
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import DialogueError, Phase, Turn
from app.dialogue.service import DialogueService
from app.main import create_app
from app.mis.http import HttpMedicalSystem
from app.mis.ports import MisError
from app.settings import Settings

IDENTITY = "пациент Тестов; Александр; Примерович; 01.01.1990"


@pytest.fixture
async def dialogue():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        booking = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()),
            MemoryBookingStore(),
            DemoSchedule(clock.now()),
            clock,
        )
        yield (
            DialogueService(
                booking, MemoryDialogueStore(), b"fictional-key-for-tests-at-least-32-bytes"
            ),
            emulator,
            clock,
        )


async def held(service, call_id="call-1"):
    await service.handle(call_id, Turn(turn_id=1, text=IDENTITY))
    await service.handle(call_id, Turn(turn_id=2, text="варианты терапевт"))
    return await service.handle(call_id, Turn(turn_id=3, text="выбрать 1"))


async def test_draft_reply_replay_and_writes_stay_closed(dialogue):
    service, emulator, _ = dialogue
    result = await held(service)
    assert result.phase == Phase.HELD and result.proposal.revision == 1
    assert result.options and len(result.options) == 3
    assert await service.handle("call-1", Turn(turn_id=3, text="выбрать 1")) == result
    assert len(service.booking.store.actions) == 1
    blocked = await service.handle("call-1", Turn(turn_id=4, text="да"))
    assert blocked.code == "WRITES_DISABLED" and emulator.write_count == 0
    assert (await service.store.inspect("call-1")).pending_turn_id is None


async def test_interpreter_can_change_without_changing_domain_or_storage(dialogue):
    service, emulator, _ = dialogue
    await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))

    class SuppliedInterpreter:
        async def interpret(self, state, turn):
            assert state.phase == Phase.IDENTIFIED
            return Command(operation=Operation.OPTIONS, specialty="терапевт")

    service.interpreter = SuppliedInterpreter()
    result = await service.handle(
        "call-1", Turn(turn_id=2, text="Текст обрабатывает подставленный адаптер.")
    )
    assert result.phase == Phase.OFFERED and len(result.options) == 3
    assert emulator.write_count == 0


async def test_partial_is_not_saved_and_different_content_conflicts(dialogue):
    service, emulator, _ = dialogue
    partial = await service.handle("call-1", Turn(turn_id=1, text=IDENTITY, is_final=False))
    assert partial.kind == "partial" and partial.version == 0
    assert (await service.store.inspect("call-1")).client_id is None
    await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    with pytest.raises(DialogueError, match="TURN_CONTENT_CONFLICT"):
        await service.handle("call-1", Turn(turn_id=1, text="помощь"))
    with pytest.raises(DialogueError, match="TURN_OUT_OF_ORDER"):
        await service.handle("call-1", Turn(turn_id=3, text="статус"))
    assert emulator.write_count == 0


async def test_expiry_and_end_release_only_local_holds(dialogue):
    service, emulator, clock = dialogue
    result = await held(service)
    clock.value += timedelta(seconds=90)
    status = await service.handle("call-1", Turn(turn_id=4, text="статус"))
    assert status.phase == Phase.IDENTIFIED and status.proposal is None
    assert await service.booking.store.available(result.proposal.slot, clock.now())
    ended = await service.end("call-1")
    assert ended.phase == Phase.ENDED
    assert await service.end("call-1") == ended
    with pytest.raises(DialogueError, match="CALL_ENDED"):
        await service.handle("call-1", Turn(turn_id=5, text="статус"))
    assert emulator.write_count == 0


async def test_patient_change_invalidates_old_reservation(dialogue):
    service, emulator, clock = dialogue
    result = await held(service)
    changed = await service.handle(
        "call-1", Turn(turn_id=4, text="пациент Тестов; Иван; Примерович; 02.01.1990")
    )
    assert changed.phase == Phase.IDENTIFIED and changed.proposal is None
    assert await service.booking.store.available(result.proposal.slot, clock.now())
    assert emulator.write_count == 0


async def test_invalid_patient_change_also_removes_old_reservation(dialogue):
    service, _, clock = dialogue
    result = await held(service)
    invalid = await service.handle("call-1", Turn(turn_id=4, text="пациент неполные данные"))
    assert invalid.phase == Phase.READY and invalid.proposal is None
    assert await service.booking.store.available(result.proposal.slot, clock.now())


async def test_concurrent_retry_does_not_repeat_processing(dialogue):
    service, _, _ = dialogue
    started, finish = asyncio.Event(), asyncio.Event()
    original = service.booking.identify
    calls = []

    async def slow(*args):
        calls.append(args)
        started.set()
        await finish.wait()
        return await original(*args)

    service.booking.identify = slow
    task = asyncio.create_task(service.handle("call-1", Turn(turn_id=1, text=IDENTITY)))
    await started.wait()
    processing = await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    assert processing.kind == "processing"
    assert (await service.store.inspect("call-1")).pending_turn_id == 1
    with pytest.raises(DialogueError, match="TURN_IN_PROGRESS"):
        await service.handle("call-1", Turn(turn_id=2, text="статус"))
    finish.set()
    result = await task
    assert result.phase == Phase.IDENTIFIED and len(calls) == 1


async def test_end_during_selection_cannot_resurrect_call_or_hold(dialogue):
    service, emulator, clock = dialogue
    await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    await service.handle("call-1", Turn(turn_id=2, text="варианты терапевт"))
    started, finish = asyncio.Event(), asyncio.Event()
    original = service.booking.propose

    async def slow(*args):
        started.set()
        await finish.wait()
        return await original(*args)

    service.booking.propose = slow
    task = asyncio.create_task(service.handle("call-1", Turn(turn_id=3, text="выбрать 1")))
    await started.wait()
    await service.end("call-1")
    finish.set()
    result = await task
    assert result.phase == Phase.ENDED
    assert (await service.store.inspect("call-1")).phase == Phase.ENDED
    assert await service.booking.store.available(service.booking.schedule.slots()[0], clock.now())
    assert emulator.write_count == 0


async def test_cancelled_worker_is_not_reexecuted(dialogue):
    service, _, _ = dialogue
    calls = []

    async def interrupted(*args):
        calls.append(args)
        raise asyncio.CancelledError()

    service.booking.identify = interrupted
    with pytest.raises(asyncio.CancelledError):
        await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    assert (await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))).kind == "processing"
    assert len(calls) == 1
    assert (await service.end("call-1")).phase == Phase.ENDED


async def test_rate_limit_wait_is_saved_without_sleeping_or_more_reads(dialogue):
    service, _, clock = dialogue
    original = service.booking.identify
    calls = []

    async def exhausted(*args):
        calls.append(args)
        raise MisError("LOCAL_RATE_LIMIT", retry_after=17)

    service.booking.identify = exhausted
    response = await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    assert response.code == "WAIT_MIS" and response.retry_at == clock.now() + timedelta(seconds=17)
    assert (await service.handle("call-1", Turn(turn_id=2, text=IDENTITY))).code == "WAIT_MIS"
    assert len(calls) == 1
    clock.value += timedelta(seconds=17)
    service.booking.identify = original
    assert (
        await service.handle("call-1", Turn(turn_id=3, text=IDENTITY))
    ).phase == Phase.IDENTIFIED


async def test_unexpected_failure_has_safe_receipt_and_stops_scenario(dialogue):
    service, _, _ = dialogue

    async def failure(*args):
        raise RuntimeError("private-input-token")

    service.booking.identify = failure
    result = await service.handle("call-1", Turn(turn_id=1, text=IDENTITY))
    assert result.phase == Phase.INTERRUPTED and "private" not in result.model_dump_json()
    assert await service.handle("call-1", Turn(turn_id=1, text=IDENTITY)) == result
    assert (
        await service.handle("call-1", Turn(turn_id=2, text="статус"))
    ).phase == Phase.INTERRUPTED


async def test_http_routes_validation_and_lifespan(dialogue):
    service, emulator, clock = dialogue

    @asynccontextmanager
    async def factory(settings, selected_clock):
        yield service

    app = create_app(
        Settings(_env_file=None, backend_demo_enabled=True), clock=clock, dialogue_factory=factory
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/dialog/call-1/turn", json={"turn_id": 1, "text": IDENTITY}
            )
            assert response.status_code == 200 and response.json()["phase"] == "identified"
            replay = await client.post(
                "/api/dialog/call-1/turn", json={"turn_id": 1, "text": IDENTITY}
            )
            assert replay.json() == response.json()
            assert (
                await client.post("/api/dialog/call-1/turn", json={"turn_id": 1, "text": "помощь"})
            ).status_code == 409
            invalid = await client.post(
                "/api/dialog/call-1/turn",
                json={"turn_id": "1", "text": IDENTITY, "private": "secret"},
            )
            assert (
                invalid.status_code == 422
                and "Александр" not in invalid.text
                and "secret" not in invalid.text
            )
            assert (await client.get("/api/dialog/call-1")).json()["last_turn_id"] == 1
            ended = await client.post("/api/dialog/call-1/end")
            assert ended.json()["phase"] == "ended"
            assert (await client.post("/api/dialog/call-1/end")).json() == ended.json()
    assert not hasattr(app.state, "dialogue") and emulator.write_count == 0


async def test_default_http_factory_is_autonomous_and_opt_in():
    for enabled in (False, True):
        app = create_app(Settings(_env_file=None, backend_demo_enabled=enabled), clock=DemoClock())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/dialog/new/turn", json={"turn_id": 1, "text": "помощь"}
                )
                assert response.status_code == (200 if enabled else 404)


def test_demo_requires_offline_and_persistent_hash_key():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None, backend_demo_enabled=True, app_mode="hackathon", mis_token="fictional"
        )
    with pytest.raises(ValueError):
        Settings(_env_file=None, backend_demo_enabled=True, dialogue_storage="postgres")
    settings = Settings(
        _env_file=None,
        backend_demo_enabled=True,
        dialogue_storage="postgres",
        dialogue_hash_key="fictional-key-for-tests-at-least-32-bytes",
    )
    assert "fictional-key" not in repr(settings)
