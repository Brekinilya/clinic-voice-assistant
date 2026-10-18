"""Независимые регрессии приёмки A03; ожидают безопасное поведение контракта."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.clock import CLINIC_TIMEZONE
from app.dialogue.models import Turn
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

    async def delayed(task):
        entered.set()
        await release.wait()
        return await original(task)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(service.callback_turn(call, Turn(turn_id=5, text="да")))
    await entered.wait()
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
