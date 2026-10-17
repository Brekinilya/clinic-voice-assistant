"""Контракт хранилищ и реальные гонки PostgreSQL; каждая проверка в отдельной схеме."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError
from app.booking.service import BookingService
from app.demo import DemoClock, DemoSchedule, EmulatorBudget, demo_emulator
from app.mis.http import HttpMedicalSystem
from app.mis.ports import MisError
from app.storage.booking import (
    PostgresBookingStore,
    PostgresBudget,
    actions,
    budget_requests,
    migrate,
    schema_versions,
)
from app.storage.schema import SCHEMA_VERSION


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    if request.param == "memory":
        return MemoryBookingStore()
    engine = request.getfixturevalue("pg_engine")
    return PostgresBookingStore(engine)


def approval(action):
    return Approval(
        call_id=action.call_id,
        action_id=action.action_id,
        revision=action.revision,
        turn_id=4,
        is_final=True,
        permitted=True,
    )


async def test_store_slot_race_and_patient_specialty_race(store):
    clock = DemoClock()
    schedule = DemoSchedule(clock.now())
    results = await asyncio.gather(
        *(store.hold(f"call-{i}", i + 1, schedule.slots()[0], clock.now()) for i in range(10)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(
        isinstance(r, BookingError) and str(r) == "SLOT_BUSY"
        for r in results
        if isinstance(r, Exception)
    )
    winner = next(r for r in results if not isinstance(r, Exception))
    with pytest.raises(BookingError, match="SPECIALTY_ALREADY_BOOKED"):
        await store.hold("new-call", winner.client_id, schedule.slots()[1], clock.now())


async def test_store_claim_is_atomic_and_survives_expiry(store):
    clock = DemoClock()
    slot = DemoSchedule(clock.now()).slots()[0]
    action = await store.hold("call-1", 1, slot, clock.now())
    results = await asyncio.gather(*(store.claim(approval(action), clock.now()) for _ in range(12)))
    assert sum(claimed for _, claimed in results) == 1
    clock.value += timedelta(hours=1)
    assert not await store.available(slot, clock.now())
    current, claimed = await store.claim(approval(action), clock.now())
    assert current.state == ActionState.COMMITTING and not claimed
    with pytest.raises(BookingError):
        await store.cancel(action.action_id, action.call_id)
    await store.finish(action.action_id, ActionState.RECONCILING)
    assert not await store.available(slot, clock.now())


async def test_store_expiry_is_exact_and_failed_replacement_keeps_old_hold(store):
    clock = DemoClock()
    schedule = DemoSchedule(clock.now())
    action = await store.hold("call-1", 1, schedule.slots()[0], clock.now())
    other = await store.hold("call-2", 2, schedule.slots()[1], clock.now())
    with pytest.raises(BookingError, match="SLOT_BUSY"):
        await store.hold("call-1", 1, other.slot, clock.now())
    assert (await store.get(action.action_id)).state == ActionState.HELD
    assert not await store.available(action.slot, clock.now() + timedelta(seconds=89))
    assert await store.available(action.slot, clock.now() + timedelta(seconds=90))
    with pytest.raises(BookingError, match="HOLD_NOT_ACTIVE"):
        await store.claim(approval(action), clock.now() + timedelta(seconds=90))


async def test_store_change_revision_and_confirmation_binding(store):
    clock = DemoClock()
    schedule = DemoSchedule(clock.now())
    action = await store.hold("call-1", 1, schedule.slots()[0], clock.now())
    changed = await store.hold("call-1", 1, schedule.slots()[1], clock.now())
    assert changed.revision == action.revision + 1
    with pytest.raises(BookingError):
        await store.claim(approval(action), clock.now())
    await store.claim(approval(changed), clock.now())
    with pytest.raises(BookingError, match="STALE_CONFIRMATION"):
        await store.claim(approval(changed).model_copy(update={"turn_id": 5}), clock.now())
    with pytest.raises(BookingError, match="ACTION_PENDING"):
        await store.hold("call-1", 1, schedule.slots()[2], clock.now())


async def test_store_only_verified_success_has_visit_id(store):
    clock = DemoClock()
    action = await store.hold("call-1", 1, DemoSchedule(clock.now()).slots()[0], clock.now())
    with pytest.raises(BookingError, match="INVALID_TRANSITION"):
        await store.finish(action.action_id, ActionState.BOOKED, 10)
    await store.claim(approval(action), clock.now())
    with pytest.raises(BookingError, match="VISIT_ID_REQUIRED"):
        await store.finish(action.action_id, ActionState.BOOKED)
    result = await store.finish(action.action_id, ActionState.BOOKED, 10)
    assert (await store.claim(approval(action), clock.now())) == (result, False)
    assert await store.finish(action.action_id, ActionState.BOOKED, 10) == result
    with pytest.raises(BookingError, match="INVALID_TRANSITION"):
        await store.finish(action.action_id, ActionState.REJECTED)


async def test_store_past_booking_does_not_block_future_specialty(store):
    clock = DemoClock()
    schedule = DemoSchedule(clock.now())
    action = await store.hold("call-1", 1, schedule.slots()[0], clock.now())
    await store.claim(approval(action), clock.now())
    await store.finish(action.action_id, ActionState.BOOKED, 10)
    clock.value += timedelta(days=2)
    next_action = await store.hold("call-2", 1, schedule.slots()[2], clock.now())
    assert next_action.state == ActionState.HELD
    past, claimed = await store.claim(approval(action), clock.now())
    assert past.state == ActionState.ARCHIVED and past.visit_id == 10 and not claimed


@pytest.mark.postgres
async def test_real_database_constraints_and_idempotent_migration(pg_engine):
    await migrate(pg_engine)
    async with pg_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
    clock = DemoClock()
    store = PostgresBookingStore(pg_engine)
    action = await store.hold("call-1", 1, DemoSchedule(clock.now()).slots()[0], clock.now())
    async with pg_engine.connect() as connection:
        original = dict((await connection.execute(select(actions))).mappings().one())
    original.update(action_id=uuid4().hex, call_id="bypass-lock", client_id=2)
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            await connection.execute(insert(actions).values(**original))
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(actions)
                .where(actions.c.action_id == action.action_id)
                .values(state="booked")
            )


@pytest.mark.postgres
async def test_restart_recovery_with_emulator_after_lost_response(pg_engine):
    clock = DemoClock()
    emulator = demo_emulator(clock)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        service = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()),
            PostgresBookingStore(pg_engine),
            DemoSchedule(clock.now()),
            clock,
        )
        action = await service.propose("call-1", 1, service.schedule.slots()[0])
        emulator.next_failure = "lost_response"
        assert (await service.confirm(approval(action))).state == ActionState.RECONCILING
        # Новый объект приложения читает тот же журнал, без состояния прежнего сервиса.
        restarted = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()),
            PostgresBookingStore(pg_engine),
            service.schedule,
            clock,
        )
        assert (await restarted.confirm(approval(action))).state == ActionState.RECONCILING
        assert emulator.write_count == 1
        assert (await restarted.reconcile(action.action_id)).state == ActionState.BOOKED
        assert emulator.write_count == 1


@pytest.mark.postgres
async def test_real_budget_is_shared_and_survives_new_objects(pg_engine):
    budgets = [PostgresBudget(pg_engine, "fictional-team-token") for _ in range(12)]
    results = await asyncio.gather(
        *(budget.reserve() for budget in budgets), return_exceptions=True
    )
    assert sum(r is None for r in results) == 8
    assert all(
        isinstance(r, MisError) and r.retry_after >= 1 for r in results if isinstance(r, Exception)
    )
    restarted = PostgresBudget(pg_engine, "fictional-team-token")
    with pytest.raises(MisError):
        await restarted.reserve()
    async with pg_engine.begin() as connection:
        # Проверяем границу окна без минутного ожидания.
        await connection.execute(
            update(budget_requests).values(
                requested_at=func.clock_timestamp() - text("INTERVAL '61 seconds'")
            )
        )
    await restarted.reserve()
    await restarted.defer(120)
    with pytest.raises(MisError) as error:
        await PostgresBudget(pg_engine, "fictional-team-token").reserve()
    assert error.value.retry_after >= 119
    await PostgresBudget(pg_engine, "different-fictional-token").reserve()
