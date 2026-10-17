"""Общий контракт журналов: конкуренция, барьер, истечение и сохранность миграции."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update

from app.booking.memory import MemoryBookingStore
from app.booking.models import Approval, BookingError
from app.cancellation.memory import MemoryCancellationStore
from app.cancellation.models import CancelProposal, CancelState, summary
from app.demo import DemoClock, demo_emulator
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import Phase, reply
from app.mis.models import Visit, VisitStatus
from app.storage.booking import migrate
from app.storage.cancellation import PostgresCancellationStore
from app.storage.dialogue import PostgresDialogueStore
from app.storage.schema import (
    SCHEMA_VERSION,
    cancellations,
    delivery_journal,
    metadata,
    moves,
    outbox_jobs,
    schema_versions,
)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def cancellation_stores(request):
    clock = DemoClock()
    emulator = demo_emulator(clock)
    visit = Visit(
        id=1,
        client_id=1,
        doctor=emulator.doctors[1],
        scheduled_at=clock.now() + timedelta(days=1),
        status=VisitStatus.PLANNED,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    if request.param == "memory":
        dialogue = MemoryDialogueStore()
        store = MemoryCancellationStore(MemoryBookingStore().ledger, dialogue)
    else:
        engine = request.getfixturevalue("pg_engine")
        dialogue, store = PostgresDialogueStore(engine), PostgresCancellationStore(engine)
    return dialogue, store, clock, visit


async def permitted(stores):
    dialogue, store, clock, visit = stores
    action = await store.prepare("cancel-call", visit, clock.now())
    await dialogue.observe("cancel-call", 1, "prompt")
    first = await dialogue.claim("cancel-call", 1, "prompt")
    state = first.state.model_copy(
        update={
            "client_id": 1,
            "purpose": "cancel",
            "phase": Phase.CANCEL_CONFIRM,
            "cancel_proposal": CancelProposal(
                action_id=action.action_id,
                revision=action.revision,
                visit=summary(visit),
                expires_at=action.expires_at,
            ),
        }
    )
    await dialogue.complete(state, 1, reply(state, 1, "Отменить запись?"))
    await dialogue.observe("cancel-call", 2, "yes")
    claim = await dialogue.claim("cancel-call", 2, "yes")
    assert await dialogue.authorize("cancel-call", claim.state.version, claim.state.input_epoch, 2)
    approval = Approval(
        call_id="cancel-call",
        action_id=action.action_id,
        revision=action.revision,
        turn_id=2,
        is_final=True,
        permitted=True,
        guard_version=claim.state.version,
        input_epoch=claim.state.input_epoch,
    )
    return approval


async def test_concurrent_claim_has_one_owner_and_committing_survives_end(cancellation_stores):
    dialogue, store, clock, _ = cancellation_stores
    approval = await permitted(cancellation_stores)
    results = await asyncio.gather(*(store.claim(approval, clock.now()) for _ in range(6)))
    assert sum(claimed for _, claimed in results) == 1
    await dialogue.end("cancel-call")
    await store.release_call("cancel-call")
    assert (await store.get(approval.action_id)).state == CancelState.COMMITTING


@pytest.mark.parametrize("change", ["partial", "end", "patient", "visit"])
async def test_revoked_or_forged_cancel_stamp_cannot_claim(cancellation_stores, change):
    dialogue, store, clock, _ = cancellation_stores
    approval = await permitted(cancellation_stores)
    if change == "partial":
        await dialogue.observe("cancel-call", 3, "different")
    elif change == "end":
        await dialogue.end("cancel-call")
    else:
        # Имитация повреждённого состояния: подпись осталась, а показанная цель изменилась.
        state = await dialogue.inspect("cancel-call")
        if change == "patient":
            state = state.model_copy(update={"client_id": 2})
        else:
            proposal = state.cancel_proposal.model_copy(
                update={"visit": state.cancel_proposal.visit.model_copy(update={"visit_id": 2})}
            )
            state = state.model_copy(update={"cancel_proposal": proposal})
        if isinstance(dialogue, MemoryDialogueStore):
            dialogue.states[state.call_id] = state
        else:
            from app.storage.schema import dialogue_sessions

            async with dialogue.engine.begin() as connection:
                await connection.execute(
                    update(dialogue_sessions)
                    .where(dialogue_sessions.c.call_id == state.call_id)
                    .values(state=state.model_dump(mode="json"))
                )
    with pytest.raises(BookingError, match="CONFIRMATION_INTERRUPTED"):
        await store.claim(approval, clock.now())
    assert (await store.get(approval.action_id)).state == CancelState.PREPARED


async def test_same_visit_is_reserved_once_and_expiry_releases_it(cancellation_stores):
    _, store, clock, visit = cancellation_stores
    results = await asyncio.gather(
        store.prepare("a", visit, clock.now()),
        store.prepare("b", visit, clock.now()),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, BookingError) for r in results) == 1
    clock.value += timedelta(seconds=90)
    assert (await store.prepare("c", visit, clock.now())).state == CancelState.PREPARED


async def test_restore_unsent_preserves_original_deadline(cancellation_stores):
    _, store, clock, _ = cancellation_stores
    approval = await permitted(cancellation_stores)
    before = await store.get(approval.action_id)
    await store.claim(approval, clock.now())
    restored = await store.restore_unsent(approval.action_id, approval.turn_id)
    assert restored.expires_at == before.expires_at and restored.confirmed_turn is None
    assert restored.state == CancelState.PREPARED


@pytest.mark.postgres
async def test_migration_two_preserves_dialogue(pg_blank_engine):
    legacy = [
        t
        for t in metadata.sorted_tables
        if t not in (cancellations, outbox_jobs, delivery_journal, moves)
    ]
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=legacy))
        await connection.execute(insert(schema_versions).values(version=2))
    dialogue = PostgresDialogueStore(pg_blank_engine)
    await dialogue.observe("old-call", 1, "old")
    before = await dialogue.inspect("old-call")
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    assert await dialogue.inspect("old-call") == before
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
        assert list((await connection.execute(select(cancellations))).all()) == []
