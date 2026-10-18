"""Миграция с версии 6 сохраняет операцию; очередь и старые владельцы проверены в БД."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.booking.models import ActionState, Approval
from app.demo import DemoClock, DemoSchedule
from app.recovery.models import Outcome, OwnershipLost
from app.storage.booking import PostgresBookingStore, migrate
from app.storage.recovery import PostgresRecoveryStore
from app.storage.schema import SCHEMA_VERSION, recovery_operations, schema_versions


@pytest.mark.postgres
async def test_migration_from_six_keeps_unknown_operation_and_backfills_once(pg_blank_engine):
    await migrate(pg_blank_engine)
    clock = DemoClock()
    bookings = PostgresBookingStore(pg_blank_engine)
    action = await bookings.hold("migration", 1, DemoSchedule(clock.now()).slots()[0], clock.now())
    approval = Approval(
        call_id=action.call_id,
        action_id=action.action_id,
        revision=1,
        turn_id=1,
        is_final=True,
        permitted=True,
    )
    await bookings.claim(approval, clock.now())
    async with pg_blank_engine.begin() as connection:
        await connection.execute(text("DROP TABLE backend_recovery_operations"))
        await connection.execute(update(schema_versions).values(version=6))
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    assert (await bookings.get(action.action_id)).state == ActionState.COMMITTING
    queue = PostgresRecoveryStore(pg_blank_engine)
    assert await queue.seed(clock.now()) == 1
    assert await queue.seed(clock.now()) == 0
    async with pg_blank_engine.connect() as connection:
        assert (
            await connection.execute(select(schema_versions.c.version))
        ).scalar_one() == SCHEMA_VERSION
        assert len((await connection.execute(select(recovery_operations))).all()) == 1


@pytest.mark.postgres
async def test_independent_stores_fence_old_owner_and_keep_manual_on_restart(pg_engine):
    clock = DemoClock()
    first, second = PostgresRecoveryStore(pg_engine), PostgresRecoveryStore(pg_engine)
    old = await first.acquire("booking", "one", clock.now(), seconds=10)
    assert await second.acquire("booking", "one", clock.now()) is None
    clock.value += timedelta(seconds=11)
    claimed = await asyncio.gather(first.claim_next(clock.now()), second.claim_next(clock.now()))
    assert sum(lease is not None for lease in claimed) == 1
    new = next(lease for lease in claimed if lease is not None)
    with pytest.raises(OwnershipLost):
        await first.finish(old, clock.now(), Outcome(state="resolved"))
    await second.finish(new, clock.now(), Outcome(state="manual", reason="AMBIGUOUS_VISITS"))
    restarted = PostgresRecoveryStore(pg_engine)
    rows = await restarted.list(state="manual")
    assert len(rows) == 1 and rows[0].reason == "AMBIGUOUS_VISITS"
    assert await restarted.claim_next(clock.now() + timedelta(days=1)) is None
