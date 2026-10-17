"""Версия 4 → 5 сохраняет очередь; завершение переноса полностью откатывается при ошибке."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from test_rescheduling_store import move_store_engine as move_store_engine
from test_rescheduling_store import move_stores as move_stores
from test_rescheduling_store import permitted

from app.booking.models import ActionState, BookingError
from app.cancellation.models import CancelAction, CancelState
from app.demo import DemoClock, demo_emulator
from app.mis.models import Visit, VisitStatus
from app.outbox.delivery import journal_entry, journal_receipt
from app.outbox.models import JobState, cancellation_events
from app.rescheduling.models import MoveState
from app.storage.booking import migrate
from app.storage.outbox import PostgresOutboxStore, values
from app.storage.schema import (
    SCHEMA_VERSION,
    cancellations,
    delivery_journal,
    metadata,
    moves,
    outbox_jobs,
    schema_versions,
)


@pytest.mark.postgres
async def test_real_legacy_kind_check_and_accepted_receipt_survive_migration(pg_blank_engine):
    clock = DemoClock()
    before = Visit(
        id=1,
        client_id=1,
        doctor=demo_emulator(clock).doctors[1],
        scheduled_at=clock.now(),
        status=VisitStatus.PLANNED,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    audit = CancelAction(
        action_id=uuid4().hex,
        call_id="legacy",
        revision=1,
        before=before,
        expires_at=clock.now(),
        state=CancelState.CANCELLED,
        completed_at=clock.now(),
    )
    jobs = list(cancellation_events(audit, clock.now()))
    jobs[0] = jobs[0].model_copy(
        update={"state": JobState.ACKNOWLEDGED, "receipt": journal_receipt(jobs[0].event_id)}
    )
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: metadata.create_all(
                sync, tables=[t for t in metadata.sorted_tables if t is not moves]
            )
        )
        await connection.execute(insert(schema_versions).values(version=4))
        await connection.execute(
            text("ALTER TABLE backend_outbox_jobs DROP CONSTRAINT backend_outbox_kind_allowed")
        )
        await connection.execute(
            text(
                "ALTER TABLE backend_outbox_jobs ADD CONSTRAINT legacy_kind_allowed CHECK (kind IN ('cancellation_sms','slot_released'))"
            )
        )
        await connection.execute(
            insert(cancellations).values(
                action_id=audit.action_id,
                call_id=audit.call_id,
                revision=1,
                visit_id=1,
                client_id=1,
                snapshot=before.model_dump(mode="json"),
                expires_at=clock.now(),
                state="cancelled",
                late=0,
                completed_at=clock.now(),
            )
        )
        for job in jobs:
            await connection.execute(insert(outbox_jobs).values(**values(job)))
        await connection.execute(
            insert(delivery_journal).values(
                event_id=jobs[0].event_id,
                entry=journal_entry(jobs[0], clock).model_dump(mode="json"),
            )
        )
    store = PostgresOutboxStore(pg_blank_engine)
    prior = await store.list()
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    assert await store.list() == prior
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
        assert (await connection.execute(select(delivery_journal.c.event_id))).scalar_one() == jobs[
            0
        ].event_id
        assert list((await connection.execute(select(moves))).all()) == []


@pytest.mark.postgres
@pytest.mark.parametrize("move_store_engine", ["postgres"], indirect=True)
async def test_outbox_error_rolls_back_audit_new_booking_and_archiving(move_stores, monkeypatch):
    _, booking, moving, cancelling, clock, before, slot, old = move_stores
    action = await moving.prepare("move-call", before, slot, clock.now())
    approval = await permitted(move_stores, action)
    await moving.claim(approval, clock.now())
    new = before.model_copy(update={"id": 2, "scheduled_at": slot.scheduled_at})
    await moving.created(action.action_id, new)
    await moving.claim(approval, clock.now(), cancelling=True)
    cancelled = before.model_copy(update={"status": VisitStatus.CANCELLED})
    from app.storage import rescheduling as module

    original = module.enqueue_cancellation

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("сбой после добавления обоих событий")

    monkeypatch.setattr(module, "enqueue_cancellation", fail_after_insert)
    with pytest.raises(RuntimeError):
        await moving.complete(action.action_id, cancelled, new, clock.now())
    assert (await moving.get(action.action_id)).state == MoveState.CANCELLING
    assert (await booking.get(old.action_id)).state == ActionState.BOOKED
    with pytest.raises(BookingError, match="ACTION_NOT_FOUND"):
        await booking.get(action.action_id)
    with pytest.raises(BookingError, match="ACTION_NOT_FOUND"):
        await cancelling.get(action.action_id)
    assert await PostgresOutboxStore(moving.engine).list() == ()
    monkeypatch.setattr(module, "enqueue_cancellation", original)
    assert (
        await moving.complete(action.action_id, cancelled, new, clock.now())
    ).state == MoveState.RESCHEDULED
    assert len(await PostgresOutboxStore(moving.engine).list()) == 2
