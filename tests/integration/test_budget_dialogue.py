"""T05 через текстовый API: понимание без МИС, ограниченное ожидание и отмена записи."""

import asyncio
from datetime import timedelta

from fixture_bridge import pipeline, turn

from app.mis.budget import MemoryBudget, Priority, demand
from app.mis.ports import BudgetExhausted

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"


async def reserve(client):
    await turn(client, 1, "Запишите к терапевту на пятницу")
    await turn(client, 2, IDENTITY)
    return await turn(client, 3, "В десять тридцать")


async def test_booking_without_a_specialty_asks_the_specialty_before_identity():
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, "Запишите меня")
        assert result["code"] == "SPECIALTY_REQUIRED"
        assert result["phase"] == "ready"
        assert "к какому специалисту" in " ".join(result["utterances"]).casefold()
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.phase.value == "ready"
        assert all(entry[1:3] == ("GET", "doctors") for entry in emulator.log)
        assert emulator.write_count == 0


def directory_reads(emulator, since: int) -> list:
    return [entry for entry in emulator.log[since:] if entry[2] == "doctors"]


async def test_yes_date_and_operator_are_understood_while_mis_is_closed():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        reserved = await reserve(client)
        assert reserved["phase"] == "held"
        # Стенд ответил 429: окно закрыто для всех запросов команды.
        await budget.defer(40)
        mark = len(emulator.log)
        result = await turn(client, 4, "Да")
        assert result["code"] == "WAIT_MIS" and result["phase"] == "held"
        assert "подтвердите ещё раз" in " ".join(result["utterances"])
        assert result["retry_at"] and result["wait_deadline"]
        refined = await turn(client, 5, "Лучше в понедельник")
        assert refined["code"] == "WAIT_MIS" and refined["proposal"] is None
        current = await service.store.inspect("scenario")
        assert current.preferences.date_from.isoweekday() == 1
        operator = await turn(client, 6, "Позовите оператора")
        assert operator["transfer"] is not None and operator["phase"] == "handoff"
        # Понимание не обращалось к справочнику: запросов к МИС вообще не было.
        assert emulator.log[mark:] == [] and emulator.write_count == 0


async def test_after_a_wait_in_the_offer_once_more_brings_the_options():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        await turn(client, 1, "Запишите к терапевту на пятницу")

        async def close_after_identity(response):
            if response.request.url.path.endswith("/clients"):
                await budget.defer(30)

        hooks = service.booking.mis._client.event_hooks["response"]
        hooks.append(close_after_identity)
        waiting = await turn(client, 2, IDENTITY)
        hooks.remove(close_after_identity)
        assert waiting["code"] == "WAIT_MIS" and waiting["phase"] == "identified"
        assert "скажите «ещё раз»" in " ".join(waiting["utterances"])
        clock.value += timedelta(seconds=30)
        offered = await turn(client, 3, "Ещё раз")
        assert offered["phase"] == "offered" and offered["options"]


async def test_wait_before_identification_asks_for_the_identity_again():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await budget.defer(30)
        waiting = await turn(client, 2, IDENTITY)
        assert waiting["code"] == "WAIT_MIS" and waiting["phase"] == "identifying"
        assert "назовите ФИО и дату рождения ещё раз" in " ".join(waiting["utterances"])
        clock.value += timedelta(seconds=30)
        assert (await turn(client, 3, IDENTITY))["phase"] == "offered"


async def test_red_flag_and_refusal_do_not_need_the_directory():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        await budget.defer(600)
        emergency = await turn(client, 1, "У меня сильно давит в груди и отдаёт в руку")
        assert emergency["code"] == "EMERGENCY" and emergency["transfer"]["route"] == "R-01"
        assert directory_reads(emulator, 0) == []


async def test_wait_is_explained_bounded_and_then_handed_to_registrar():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        reserved = await reserve(client)
        started = clock.now()
        await budget.defer(50)
        waiting = await turn(client, 4, "Да")
        assert waiting["code"] == "WAIT_MIS"
        assert "около 50 секунд" in " ".join(waiting["utterances"])
        assert waiting["wait_deadline"] == (started + timedelta(seconds=120)).isoformat()
        # Повтор той же реплики возвращает квитанцию, а не новое ожидание.
        assert await turn(client, 4, "Да") == waiting
        # Стенд снова отказал: обещание вышло бы за две минуты ожидания без движения.
        clock.value += timedelta(seconds=50)
        await budget.defer(80)
        expired = await turn(client, 5, "Да")
        assert expired["code"] == "MIS_WAIT_EXPIRED" and expired["transfer"]["route"] == "R-06"
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        assert action.state == "cancelled" and emulator.write_count == 0


async def test_long_retry_after_hands_over_at_once():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        await reserve(client)
        await budget.defer(600)
        result = await turn(client, 4, "Да")
        assert result["code"] == "MIS_WAIT_EXPIRED" and emulator.write_count == 0


async def test_new_confirmation_after_the_wait_books_once_with_fresh_checks():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        service.booking.mis._budget = budget
        await reserve(client)
        await budget.defer(30)
        assert (await turn(client, 4, "Да"))["code"] == "WAIT_MIS"
        clock.value += timedelta(seconds=30)
        mark = len(emulator.log)
        booked = await turn(client, 5, "Да")
        assert booked["code"] == "BOOKED" and emulator.write_count == 1
        # Перед POST — свежие врачи профиля, день врача и записи пациента.
        assert [(m, p) for _, m, p, _ in emulator.log[mark:]] == [
            ("GET", "doctors"),
            ("GET", "visits"),
            ("GET", "visits"),
            ("POST", "visits"),
        ]
        current = await service.store.inspect("scenario")
        assert current.mis_wait_since is None and current.retry_at is None


async def held_with_short_wait(client, service, clock):
    """Бронь есть, окно занято; места освободятся через 5 секунд — короче срока хода."""
    gate = {"waiting": asyncio.Event(), "resume": asyncio.Event()}

    async def sleep(seconds):
        gate["waiting"].set()
        await gate["resume"].wait()
        clock.value += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    budget = MemoryBudget(clock, sleep=sleep)
    service.booking.mis._budget = budget
    reserved = await reserve(client)
    clock.value += timedelta(seconds=55)
    with demand(Priority.COMMIT):
        while True:
            try:
                await budget.reserve()
            except BudgetExhausted:
                break
    return reserved, gate


async def test_new_speech_during_the_wait_forbids_the_deferred_write():
    async with pipeline() as (client, service, emulator, clock, *_):
        reserved, gate = await held_with_short_wait(client, service, clock)
        pending = asyncio.create_task(turn(client, 4, "Да"))
        await asyncio.wait_for(gate["waiting"].wait(), 5)
        response = await client.post("/api/dialog/scenario/input-started", json={"turn_id": 5})
        assert response.status_code == 200
        gate["resume"].set()
        result = await pending
        assert result["code"] == "TURN_INTERRUPTED" and emulator.write_count == 0
        # Прерванный ход снимает бронь: исполнять старое «да» после паузы не на чем.
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        assert action.state == "cancelled" and action.confirmed_turn is None
        assert (await service.store.inspect("scenario")).authorization is None


async def test_hangup_during_the_wait_forbids_the_deferred_write():
    async with pipeline() as (client, service, emulator, clock, *_):
        reserved, gate = await held_with_short_wait(client, service, clock)
        pending = asyncio.create_task(turn(client, 4, "Да"))
        await asyncio.wait_for(gate["waiting"].wait(), 5)
        assert (await client.post("/api/dialog/scenario/end")).status_code == 200
        gate["resume"].set()
        result = await pending
        assert result["code"] == "CALL_ENDED" and emulator.write_count == 0
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        assert action.state == "cancelled"


async def test_short_wait_without_new_speech_still_books():
    async with pipeline() as (client, service, emulator, clock, *_):
        reserved, gate = await held_with_short_wait(client, service, clock)
        gate["resume"].set()
        result = await turn(client, 4, "Да")
        assert result["code"] == "BOOKED" and emulator.write_count == 1
