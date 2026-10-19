"""T08-c: задачи обратного звонка по §7.6 — сроки, одна задача на звонок, попытки сотрудника."""

import asyncio
from argparse import Namespace
from datetime import date, datetime, timedelta

import pytest

from app.callbacks.cli import run
from app.callbacks.memory import MemoryCallbackStore
from app.callbacks.models import CLAIM, deadline, promise
from app.callbacks.service import CallbackService
from app.clock import CLINIC_TIMEZONE
from app.storage.callbacks import PostgresCallbackStore

HOLIDAYS = frozenset({date(2026, 11, 4)})


def at(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=CLINIC_TIMEZONE)


@pytest.mark.parametrize(
    "moment, priority, due, out",
    [
        (at(10, 20, 11), "P2", at(10, 20, 11, 15), False),
        (at(10, 20, 11), "P3", at(10, 20, 11, 30), False),
        # Срок попал бы на закрытие регистратуры в 20:00 — утро следующего рабочего дня
        (at(10, 20, 19, 50), "P2", at(10, 21, 10), True),
        # Ночью — 10:00 этого же дня
        (at(10, 20, 1), "P3", at(10, 20, 10), True),
        # Суббота после 15:00 и воскресенье — понедельник
        (at(10, 24, 14, 50), "P2", at(10, 26, 10), True),
        (at(10, 25, 12), "P3", at(10, 26, 10), True),
        # 4 ноября — праздник
        (at(11, 3, 21), "P3", at(11, 5, 10), True),
    ],
)
def test_deadline_follows_section_7_6(moment, priority, due, out):
    assert deadline(priority, moment, HOLIDAYS) == (due, out)


def test_emergency_is_never_replaced_by_a_callback():
    with pytest.raises(ValueError):
        deadline("P1", at(10, 20, 11))


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


@pytest.fixture(params=["memory", "postgres"])
def callback_store(request):
    if request.param == "memory":
        return MemoryCallbackStore()
    return PostgresCallbackStore(request.getfixturevalue("pg_engine"))


@pytest.fixture
def service(callback_store):
    return CallbackService(callback_store, Clock(at(10, 20, 11)), HOLIDAYS)


async def test_one_call_and_route_is_one_task_with_the_same_promise(service):
    task, said = await service.schedule("call-1", "R-03", "P2", escalation_ref="call-1:R-03")
    again, said_again = await service.schedule("call-1", "R-03", "P2")
    assert task.id == again.id and said == said_again == "в течение 15 минут"
    assert again.escalation_ref == "call-1:R-03"
    assert [t.id for t in await service.queue()] == ["call-1:R-03"]


async def test_concurrent_scheduling_creates_one_task(service):
    await asyncio.gather(*(service.schedule("call-1", "R-05", "P3") for _ in range(5)))
    assert len(await service.queue()) == 1


async def test_out_of_hours_promise_names_the_morning(callback_store):
    service = CallbackService(callback_store, Clock(at(10, 24, 18)), HOLIDAYS)
    task, said = await service.schedule("call-2", "R-15", "P3", preferred="после обеда")
    assert task.out_of_hours and said == "в понедельник до десяти утра"
    assert task.preferred == "после обеда"
    late = CallbackService(callback_store, Clock(at(10, 20, 21)), HOLIDAYS)
    assert (await late.schedule("call-3", "R-03", "P2"))[1] == "завтра до десяти утра"


async def test_three_unanswered_attempts_need_sms_and_leave_the_queue(service):
    task, _ = await service.schedule("call-1", "R-03", "P2")
    for attempt in range(1, 4):
        claimed = await service.claim(task.id, "registrar-1")
        assert claimed.state == "calling"
        done = await service.result(task.id, "registrar-1", answered=False)
        assert done.attempts == attempt
    assert (done.state, done.sms_pending) == ("failed", True)
    assert await service.queue() == []


async def test_answered_callback_is_done_and_marks_once(service):
    task, _ = await service.schedule("call-1", "R-03", "P2")
    await service.claim(task.id, "registrar-1")
    assert (await service.result(task.id, "registrar-1", answered=True)).state == "done"
    # Повтор отметки ничего не меняет
    assert await service.result(task.id, "registrar-1", answered=False) is None


async def test_claimed_task_belongs_to_one_registrar_until_the_claim_expires(service):
    task, _ = await service.schedule("call-1", "R-03", "P2")
    assert await service.claim(task.id, "registrar-1")
    assert await service.claim(task.id, "registrar-2") is None
    assert await service.result(task.id, "registrar-2", answered=True) is None
    service.clock.value += CLAIM + timedelta(seconds=1)
    assert [t.id for t in await service.queue()] == [task.id]
    assert (await service.claim(task.id, "registrar-2")).owner == "registrar-2"
    # Прежний сотрудник уже не может отметить исход
    assert await service.result(task.id, "registrar-1", answered=True) is None


async def test_overdue_tasks_are_visible(service):
    task, _ = await service.schedule("call-1", "R-03", "P2")
    assert await service.overdue() == []
    service.clock.value += timedelta(minutes=16)
    assert [t.id for t in await service.overdue()] == [task.id]


async def test_cli_shows_the_queue_without_personal_data(service):
    await service.schedule("call-1", "R-03", "P2", client_id=7, preferred="утром")
    result = await run(Namespace(command="queue", limit=20), service)
    [row] = result["tasks"]
    assert row["route"] == "R-03" and not row["overdue"] and "client_id" not in row
    with pytest.raises(ValueError):
        await run(Namespace(command="claim", id="нет", owner="r"), service)


def test_promise_for_a_task_in_working_hours():
    from app.callbacks.models import CallbackTask

    task = CallbackTask(
        id="c:R-05",
        call_id="c",
        route="R-05",
        priority="P3",
        created_at=at(10, 20, 11),
        due_at=at(10, 20, 11, 30),
        out_of_hours=False,
    )
    assert promise(task, at(10, 20, 11)) == "в течение 30 минут"
