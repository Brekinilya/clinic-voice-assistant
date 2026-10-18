"""Независимые регрессии приёмки A03; ожидают безопасное поведение контракта."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.clock import CLINIC_TIMEZONE
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import TransferRequest, Turn
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.ports import HandoffConflict
from app.handoffs.service import HandoffService, HandoffSettings, callback_deadline
from app.storage.handoffs import PostgresHandoffStore
from tests.conftest import pg_blank_engine, pg_engine  # noqa: F401


class Clock:
    value = datetime(2026, 10, 19, 10, tzinfo=CLINIC_TIMEZONE)

    def now(self):
        return self.value


@pytest_asyncio.fixture(params=["memory", "postgres"])
async def system(request, pg_engine):  # noqa: F811 - fixture name is imported to register it
    store = MemoryHandoffStore() if request.param == "memory" else PostgresHandoffStore(pg_engine)
    clock = Clock()
    service = HandoffService(
        store, object(), clock, b"x" * 32, HandoffSettings(enabled=True, callback_enabled=True)
    )
    event = SimpleNamespace(
        attempt_id="review-attempt", call_id="review-call", route="R-08", priority="P3"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await service.start_callback(event.call_id, 1, "no_answer")
    await service.callback_turn(event.call_id, Turn(turn_id=2, text="да"))
    await service.callback_turn(event.call_id, Turn(turn_id=3, text="79991234567"))
    await service.callback_turn(event.call_id, Turn(turn_id=4, text="в ближайшее время"))
    return service, store, clock, event.call_id


@pytest.mark.asyncio
async def test_new_speech_revokes_callback_before_insert(system, monkeypatch):
    service, store, clock, call = system
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(task, **authorization):
        entered.set()
        await release.wait()
        return await original(task, **authorization)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(service.callback_turn(call, Turn(turn_id=5, text="да")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await service.callback_input_started(call, 6)
    release.set()
    await writer
    record = await store.request_for_call(call)
    assert await store.get_callback_task_by_request(record.request_id) is None


@pytest.mark.asyncio
async def test_expired_operator_lease_cannot_start(system):
    service, store, clock, call = system
    await service.callback_turn(call, Turn(turn_id=5, text="да"))
    record = await store.request_for_call(call)
    task = await store.get_callback_task_by_request(record.request_id)
    claimed = await service.operator_claim(task.task_id, "operator-a")
    clock.value += timedelta(minutes=11)
    with pytest.raises(HandoffConflict):
        await service.operator_start_attempt(task.task_id, "operator-a", claimed.version)


@pytest.mark.asyncio
async def test_operator_finish_is_idempotent(system):
    service, store, clock, call = system
    await service.callback_turn(call, Turn(turn_id=5, text="да"))
    record = await store.request_for_call(call)
    task = await store.get_callback_task_by_request(record.request_id)
    claimed = await service.operator_claim(task.task_id, "operator-a")
    task, attempt = await service.operator_start_attempt(
        task.task_id, "operator-a", claimed.version
    )
    await service.operator_finish_attempt(attempt.attempt_id, "connected")
    again = await service.operator_finish_attempt(attempt.attempt_id, "connected")
    assert again.state.value == "done"


def test_next_working_day_includes_working_saturday():
    now = datetime(2026, 10, 23, 20, 10, tzinfo=CLINIC_TIMEZONE)
    assert callback_deadline(now, "P3") == datetime(2026, 10, 24, 10, tzinfo=CLINIC_TIMEZONE)


def test_next_working_day_skips_verified_holiday():
    now = datetime(2026, 11, 3, 20, 10, tzinfo=CLINIC_TIMEZONE)
    assert callback_deadline(now, "P3") == datetime(2026, 11, 5, 10, tzinfo=CLINIC_TIMEZONE)


@pytest.mark.asyncio
async def test_voice_input_start_does_not_reset_understanding_limit(system):
    """Телефон присылает начало речи перед каждой итоговой расшифровкой."""
    service, store, _, call = system
    for turn_id in range(5, 8):
        await service.callback_input_started(call, turn_id)
        result = await service.callback_turn(call, Turn(turn_id=turn_id, text="абракадабра"))
    assert result.code == "CALLBACK_UNDERSTANDING_LIMIT"
    request = await store.request_for_call(call)
    assert await store.get_callback_draft(request.request_id) is None
    assert await store.get_callback_task_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_duplicate_input_started_does_not_revoke_same_turn_twice(system):
    service, store, _, call = system
    request = await store.request_for_call(call)
    await service.callback_input_started(call, 5)
    first = await store.get_callback_draft(request.request_id)
    await service.callback_input_started(call, 5)
    second = await store.get_callback_draft(request.request_id)
    assert second == first


@pytest.mark.asyncio
async def test_repeat_confirmation_consumes_turn_without_input_start(system):
    """Текстовый клиент не обязан присылать отдельное событие начала речи."""
    service, store, _, call = system
    repeated = await service.callback_turn(call, Turn(turn_id=5, text="повторите"))
    assert repeated.code == "CALLBACK_STEP"
    confirmed = await service.callback_turn(call, Turn(turn_id=6, text="да"))
    assert confirmed.code == "CALLBACK_QUEUED"
    request = await store.request_for_call(call)
    assert await store.get_callback_task_by_request(request.request_id) is not None


@pytest.mark.asyncio
async def test_dialogue_partial_revokes_inflight_callback_consent(system, monkeypatch):
    """Промежуточный текст тоже отзывает старое согласие, даже без input_started."""
    service, store, clock, call = system
    dialogue_store = MemoryDialogueStore()
    await dialogue_store.preempt(call, 1, TransferRequest(route="R-08", priority="P3"))
    dialogue = DialogueService(
        None, dialogue_store, b"x" * 32, safety=RulesSafety(clock), handoffs=service
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(task, **authorization):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(task, **authorization)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(dialogue.handle(call, Turn(turn_id=5, text="да")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await dialogue.partial(call, Turn(turn_id=6, text="другой номер", is_final=False))
    finally:
        release.set()
        await writer
    request = await store.request_for_call(call)
    assert await store.get_callback_task_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_final_http_turn_cannot_skip_callback_turn(system):
    service, _, clock, call = system
    dialogue_store = MemoryDialogueStore()
    await dialogue_store.preempt(call, 1, TransferRequest(route="R-08", priority="P3"))
    dialogue = DialogueService(
        None, dialogue_store, b"x" * 32, safety=RulesSafety(clock), handoffs=service
    )
    result = await dialogue.handle(call, Turn(turn_id=6, text="да"))
    assert result.code == "CALLBACK_STALE"
