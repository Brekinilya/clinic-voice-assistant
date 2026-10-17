"""T05: общий бюджет МИС — приоритет, ожидание до срока, отмена, страницы и 429."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from app.demo import DemoClock, demo_emulator
from app.mis.budget import MemoryBudget, Priority, capacity, demand
from app.mis.http import HttpMedicalSystem
from app.mis.models import Doctor, Visit, VisitCreate, VisitStatus
from app.mis.ports import BudgetCancelled, BudgetExhausted, MisError


class Sleeper:
    """Подменяет asyncio.sleep: продвигает учебные часы вместо реального ожидания."""

    def __init__(self, clock):
        self.clock = clock
        self.slept = 0.0
        self.calls = 0

    async def __call__(self, seconds):
        self.calls += 1
        self.slept += seconds
        self.clock.value += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def visits_for(clock, doctor: Doctor, client_id: int, count: int) -> dict[int, Visit]:
    start = clock.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return {
        i: Visit(
            id=i,
            client_id=client_id,
            doctor=doctor,
            scheduled_at=start + timedelta(minutes=10 * i),
            status=VisitStatus.PLANNED,
            created_at=clock.now().replace(second=0, microsecond=0),
            updated_at=clock.now().replace(second=0, microsecond=0),
        )
        for i in range(1, count + 1)
    }


@pytest.fixture
async def mis():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    sleeper = Sleeper(clock)
    budget = MemoryBudget(clock, sleep=sleeper)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        yield (
            HttpMedicalSystem(client, budget, elapsed=lambda: clock.now().timestamp()),
            (
                emulator,
                clock,
                budget,
                sleeper,
            ),
        )


def test_priority_headroom_for_production_limit():
    assert [capacity(8, p) for p in Priority] == [8, 8, 7, 5]
    # Маленький учебный лимит запаса не даёт: иначе разговор остался бы без окна.
    assert {capacity(2, p) for p in Priority} == {2}


async def test_commit_gets_the_place_that_dialogue_and_background_leave():
    clock = DemoClock()
    budget = MemoryBudget(clock)
    with demand(Priority.BACKGROUND):
        for _ in range(5):
            await budget.reserve()
        with pytest.raises(BudgetExhausted):
            await budget.reserve()
    with demand(Priority.DIALOGUE):
        await budget.reserve()
        await budget.reserve()
        with pytest.raises(BudgetExhausted):
            await budget.reserve()
    with demand(Priority.COMMIT):
        await budget.reserve()
        with pytest.raises(BudgetExhausted) as error:
            await budget.reserve()
    assert error.value.retry_after == 60


async def test_wait_lasts_until_the_window_frees_but_not_past_the_deadline():
    clock = DemoClock()
    sleeper = Sleeper(clock)
    budget = MemoryBudget(clock, limit=2, sleep=sleeper)
    await budget.reserve()
    clock.value += timedelta(seconds=20)
    await budget.reserve()
    # Срок короче освобождения окна: отказ сразу, без напрасного ожидания.
    with demand(until=clock.now() + timedelta(seconds=30)):
        with pytest.raises(BudgetExhausted) as error:
            await budget.reserve()
    assert error.value.retry_after == 40 and sleeper.calls == 0
    with demand(until=clock.now() + timedelta(seconds=45)):
        assert await budget.reserve()
    assert sleeper.slept == pytest.approx(40)


async def test_promised_waits_are_served_in_the_order_they_were_given():
    clock = DemoClock()
    budget = MemoryBudget(clock, limit=2)
    await budget.reserve(2)
    with demand(owner="call-a", until=clock.now() + timedelta(seconds=5)):
        with pytest.raises(BudgetExhausted) as first:
            await budget.reserve()
    assert first.value.retry_after == 60
    clock.value += timedelta(seconds=60)
    # Окно освободилось, но одно место обещано звонку «подождите 60 секунд».
    with demand(owner="call-b", until=clock.now()):
        with pytest.raises(BudgetExhausted) as second:
            await budget.reserve(2)
    assert second.value.retry_after == 60
    # Второе место свободно и не обещано — его можно занять, работа не простаивает.
    assert await budget.reserve()
    with pytest.raises(BudgetExhausted):
        await budget.reserve()
    clock.value += timedelta(seconds=3)
    with demand(owner="call-a"):
        assert await budget.reserve()
    # Звонок А ушёл на 3 секунды позже обещанного: Б ждёт их, а не превышает окно.
    clock.value += timedelta(seconds=57)
    with demand(owner="call-b", until=clock.now()):
        with pytest.raises(BudgetExhausted) as late:
            await budget.reserve(2)
    assert late.value.retry_after == 3
    # Пока Б ждёт свои секунды, его наступившие талоны держат места от посторонних.
    clock.value += timedelta(seconds=3)
    with pytest.raises(BudgetExhausted):
        await budget.reserve()
    with demand(owner="call-b"):
        assert len(await budget.reserve(2)) == 2


async def test_earlier_promise_is_served_first_when_both_are_due():
    clock = DemoClock()
    budget = MemoryBudget(clock, limit=2)
    await budget.reserve(2)
    for call in ("call-a", "call-b"):
        with demand(owner=call, until=clock.now()):
            with pytest.raises(BudgetExhausted):
                await budget.reserve(2)
    clock.value += timedelta(seconds=61)
    # Оба талона наступили одновременно; первым обслуживается выданный раньше.
    with demand(owner="call-b"):
        with pytest.raises(BudgetExhausted):
            await budget.reserve(2)
    with demand(owner="call-a"):
        assert len(await budget.reserve(2)) == 2


async def test_promise_counts_earlier_promises_that_are_not_due_yet():
    clock = DemoClock()
    budget = MemoryBudget(clock, limit=8)
    with demand(Priority.COMMIT):
        await budget.reserve(8)
    with demand(Priority.COMMIT, owner="call-a", until=clock.now()):
        with pytest.raises(BudgetExhausted) as first:
            await budget.reserve(4)
    clock.value += timedelta(seconds=10)
    # Шести местам рядом с обещанными А четырьмя в одном окне не поместиться:
    # обещание Б — после того, как места А освободятся.
    with demand(Priority.COMMIT, owner="call-b", until=clock.now()):
        with pytest.raises(BudgetExhausted) as second:
            await budget.reserve(6)
    assert first.value.retry_after == 60 and second.value.retry_after == 110


async def test_early_return_waits_for_the_promised_time_instead_of_booking_again():
    clock = DemoClock()
    sleeper = Sleeper(clock)
    budget = MemoryBudget(clock, limit=1, sleep=sleeper)
    await budget.reserve()
    with demand(owner="call-a", until=clock.now() + timedelta(seconds=5)):
        with pytest.raises(BudgetExhausted):
            await budget.reserve()
    clock.value += timedelta(seconds=55)
    with demand(owner="call-a", until=clock.now() + timedelta(seconds=8)):
        assert await budget.reserve()
    assert sleeper.slept == pytest.approx(5)
    assert len(budget._requests) == 1


async def test_unclaimed_promise_expires_and_background_never_books():
    clock = DemoClock()
    budget = MemoryBudget(clock, limit=2)
    await budget.reserve(2)
    with demand(Priority.BACKGROUND, owner="job", until=clock.now() + timedelta(seconds=5)):
        with pytest.raises(BudgetExhausted):
            await budget.reserve()
    assert len(budget._requests) == 2
    with demand(owner="call-a", until=clock.now() + timedelta(seconds=5)):
        with pytest.raises(BudgetExhausted):
            await budget.reserve()
    assert len(budget._requests) == 3
    # Звонок не вернулся: через 20 секунд после обещанного времени место снова общее.
    clock.value += timedelta(seconds=81)
    assert await budget.reserve(2)


async def test_new_speech_cancels_the_wait_without_taking_a_place():
    clock = DemoClock()
    sleeper = Sleeper(clock)
    budget = MemoryBudget(clock, limit=1, sleep=sleeper)
    await budget.reserve()
    checks = []

    async def cancelled():
        checks.append(clock.now())
        return len(checks) > 3

    with demand(until=clock.now() + timedelta(seconds=90), cancelled=cancelled):
        with pytest.raises(BudgetCancelled):
            await budget.reserve()
    assert sleeper.slept < 60
    clock.value += timedelta(seconds=60)
    # Отменённое ожидание места не заняло: всё окно снова доступно.
    assert await budget.reserve()


async def test_nested_demand_keeps_the_stricter_deadline_and_both_cancellations():
    clock = DemoClock()
    outer_cancelled = False

    async def outer():
        return outer_cancelled

    with demand(Priority.DIALOGUE, until=clock.now() + timedelta(seconds=8), cancelled=outer):
        with demand(Priority.COMMIT, until=clock.now() + timedelta(seconds=60)) as inner:
            assert inner.priority == Priority.COMMIT
            assert inner.until == clock.now() + timedelta(seconds=8)
            outer_cancelled = True
            assert await inner.cancelled()


async def test_unused_prepaid_places_return_to_the_window(mis):
    service, (emulator, clock, budget, _) = mis
    with demand(Priority.COMMIT):
        async with service.prepaid(5, keep_for_write=1):
            await service.doctors()
    assert len(budget._requests) == 1
    with demand(Priority.COMMIT):
        for _ in range(7):
            await budget.reserve()


async def test_reads_leave_the_prepaid_place_for_the_write(mis):
    service, (emulator, clock, budget, _) = mis
    with demand(Priority.COMMIT):
        async with service.prepaid(2, keep_for_write=1):
            await service.doctors(fresh=True)
            # Второе чтение не съедает место записи и берёт окно отдельно.
            await service.doctors(fresh=True)
            await service.create_visit(
                VisitCreate(client_id=1, doctor_id=1, scheduled_at=clock.now() + timedelta(days=1))
            )
    assert len(budget._requests) == 3 and emulator.write_count == 1


async def test_pause_after_429_stops_prepaid_requests_too(mis):
    service, (emulator, clock, budget, _) = mis
    with demand(Priority.COMMIT):
        async with service.prepaid(2, keep_for_write=1):
            await budget.defer(30)
            with pytest.raises(BudgetExhausted) as error:
                await service.create_visit(
                    VisitCreate(
                        client_id=1, doctor_id=1, scheduled_at=clock.now() + timedelta(days=1)
                    )
                )
    assert error.value.retry_after == 30 and emulator.write_count == 0


async def test_write_without_prepaid_place_never_waits(mis):
    service, (emulator, clock, budget, sleeper) = mis
    with demand(Priority.COMMIT):
        for _ in range(8):
            await budget.reserve()
        with demand(until=clock.now() + timedelta(seconds=120)):
            with pytest.raises(BudgetExhausted):
                await service.create_visit(
                    VisitCreate(
                        client_id=1, doctor_id=1, scheduled_at=clock.now() + timedelta(days=1)
                    )
                )
    assert sleeper.calls == 0 and emulator.write_count == 0


async def test_more_than_eight_pages_resume_instead_of_restarting(mis):
    service, (emulator, clock, budget, _) = mis
    emulator.records["demo"] = visits_for(clock, emulator.doctors[1], 1, 460)
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted) as error:
            await service.visits(client_id=1)
    # Разговор занял свои 7 мест окна; прочитанные страницы сохранены.
    assert [offset for *_, offset in emulator.log] == [50 * i for i in range(7)]
    clock.value += timedelta(seconds=error.value.retry_after)
    with demand(owner="call-1"):
        result = await service.visits(client_id=1)
    assert len(result) == 460 and len({v.id for v in result}) == 460
    assert [offset for *_, offset in emulator.log] == [50 * i for i in range(10)]


async def test_resumed_page_skips_unchanged_card_shifted_by_a_new_visit(mis):
    service, (emulator, clock, budget, _) = mis
    records = visits_for(clock, emulator.doctors[1], 1, 120)
    emulator.records["demo"] = records
    budget._limit = 1
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted):
            await service.visits(client_id=1)
    # Новая запись раньше прочитанных сдвигает страницы на одну карточку.
    earliest = records[1].scheduled_at - timedelta(minutes=5)
    records[500] = records[1].model_copy(update={"id": 500, "scheduled_at": earliest})
    budget._limit = 8
    clock.value += timedelta(seconds=60)
    with demand(owner="call-1"):
        result = await service.visits(client_id=1)
    assert len(result) == 120 and len({v.id for v in result}) == 120


async def test_resume_belongs_to_one_owner_and_expires(mis):
    service, (emulator, clock, budget, _) = mis
    emulator.records["demo"] = visits_for(clock, emulator.doctors[1], 1, 460)
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted):
            await service.visits(client_id=1)
    clock.value += timedelta(seconds=60)
    before = len(emulator.log)
    with demand(owner="call-2"):
        with pytest.raises(BudgetExhausted):
            await service.visits(client_id=1)
    assert emulator.log[before][3] == 0
    # Через минуту без продолжения прочитанное устарело: читаем заново.
    clock.value += timedelta(seconds=120)
    before = len(emulator.log)
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted):
            await service.visits(client_id=1)
    assert emulator.log[before][3] == 0
    service.clear_call_cache("call-2")
    assert not any(key[1] == "call-2" for key in service._partial)


async def test_fresh_busy_check_never_continues_an_old_read(mis):
    service, (emulator, clock, budget, _) = mis
    emulator.records["demo"] = visits_for(clock, emulator.doctors[1], 1, 460)
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted):
            await service.visits(doctor_id=1, call_id="call-1")
    clock.value += timedelta(seconds=60)
    before = len(emulator.log)
    with demand(owner="call-1"):
        with pytest.raises(BudgetExhausted):
            await service.visits(doctor_id=1, call_id="call-1", fresh=True)
    assert emulator.log[before][3] == 0


async def test_profile_with_85_doctors_and_more_than_50_clients(mis):
    service, (emulator, clock, budget, _) = mis
    emulator.doctors = {
        i: Doctor(
            id=i,
            full_name=f"Врачова{i} Анна Тестовна",
            specialty="терапевт" if i % 2 else "хирург",
            is_active=True,
        )
        for i in range(1, 86)
    }
    template = emulator.clients["demo"][1]
    emulator.clients["demo"] = {
        i: template.model_copy(update={"id": i, "last_name": f"Посторонний{i}"})
        for i in range(1, 61)
    }
    emulator.clients["demo"][61] = template.model_copy(update={"id": 61})
    assert len(await service.doctors()) == 85
    found = await service.clients("Тестов", "Александр", template.birth_date)
    assert [c.id for c in found] == [61]
    assert len(await service.doctors("хирург")) == 42
    # Справочник прочитан двумя страницами и дальше берётся из кэша по типу данных.
    assert [(path, offset) for _, _, path, offset in emulator.log] == [
        ("doctors", 0),
        ("doctors", 50),
        ("clients", 0),
        ("clients", 50),
    ]
    assert len(service.known_doctors()) == 85
    clock.value += timedelta(seconds=601)
    await service.doctors()
    assert len(emulator.log) == 6


async def test_read_after_429_waits_for_retry_after_within_deadline(mis):
    service, (emulator, clock, budget, sleeper) = mis
    emulator.rate_limit = 1
    await service.doctors(fresh=True)
    with demand(until=clock.now() + timedelta(seconds=90)):
        assert len(await service.doctors(fresh=True)) == 1
    assert emulator.limited == 1 and sleeper.slept == pytest.approx(60)


async def test_read_after_429_without_deadline_and_write_are_not_repeated(mis):
    service, (emulator, clock, budget, sleeper) = mis
    emulator.rate_limit = 1
    await service.doctors(fresh=True)
    with pytest.raises(MisError) as error:
        await service.doctors(fresh=True)
    assert error.value.code == "RATE_LIMIT" and error.value.retry_after == 60
    # Пауза стенда закрыла окно и для записи; POST не отправлялся и не повторялся.
    with demand(Priority.COMMIT):
        with pytest.raises(BudgetExhausted):
            await service.create_visit(
                VisitCreate(client_id=1, doctor_id=1, scheduled_at=clock.now() + timedelta(days=1))
            )
    assert emulator.write_count == 0 and emulator.limited == 1


async def test_emulator_limits_requests_like_the_stand():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    emulator.rate_limit = 10
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        codes = [(await client.get("doctors")).status_code for _ in range(11)]
        limited = await client.get("doctors")
    assert codes == [200] * 10 + [429]
    assert limited.headers["Retry-After"] == "60" and emulator.limited == 2
