"""Два назначения защищены общим замком с созданием и отменой; итог атомарен."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError, Slot
from app.cancellation.memory import MemoryCancellationStore
from app.demo import DemoClock, demo_emulator
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import Phase, reply
from app.mis.models import Visit, VisitStatus
from app.rescheduling.memory import MemoryMoveStore
from app.rescheduling.models import MoveState, proposal
from app.storage.booking import PostgresBookingStore
from app.storage.cancellation import PostgresCancellationStore
from app.storage.dialogue import PostgresDialogueStore
from app.storage.rescheduling import PostgresMoveStore
from app.storage.schema import dialogue_sessions


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def move_store_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@pytest.fixture
async def move_stores(move_store_engine):
    clock = DemoClock()
    before = Visit(
        id=1,
        client_id=1,
        doctor=demo_emulator(clock).doctors[1],
        scheduled_at=clock.now() + timedelta(days=1),
        status=VisitStatus.PLANNED,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    slot = Slot(
        doctor_id=1,
        specialty=before.doctor.specialty,
        scheduled_at=before.scheduled_at + timedelta(days=1),
    )
    if move_store_engine is None:
        dialogue = MemoryDialogueStore()
        booking = MemoryBookingStore(dialogue)
        moving = MemoryMoveStore(booking.ledger, dialogue)
        cancelling = MemoryCancellationStore(booking.ledger, dialogue)
    else:
        dialogue = PostgresDialogueStore(move_store_engine)
        booking = PostgresBookingStore(move_store_engine)
        moving, cancelling = (
            PostgresMoveStore(move_store_engine),
            PostgresCancellationStore(move_store_engine),
        )
    old_slot = Slot(
        doctor_id=1, specialty=before.doctor.specialty, scheduled_at=before.scheduled_at
    )
    old = await booking.hold("original", 1, old_slot, clock.now())
    await booking.claim(
        Approval(
            call_id=old.call_id,
            action_id=old.action_id,
            revision=old.revision,
            turn_id=1,
            is_final=True,
            permitted=True,
        ),
        clock.now(),
    )
    await booking.finish(old.action_id, ActionState.BOOKED, 1)
    return dialogue, booking, moving, cancelling, clock, before, slot, old


async def permitted(stores, action, *, stage="move"):
    dialogue, _, _, _, clock, *_ = stores
    state = await dialogue.observe(action.call_id, 1, "prompt")
    claimed = await dialogue.claim(action.call_id, 1, "prompt")
    state = claimed.state.model_copy(
        update={
            "client_id": action.before.client_id,
            "purpose": "reschedule",
            "phase": Phase.MOVE_CONFIRM if stage == "move" else Phase.MOVE_FINISH_CONFIRM,
            "move_proposal": proposal(action, stage=stage),
        }
    )
    await dialogue.complete(state, 1, reply(state, 1, "Подтвердите перенос."))
    await dialogue.observe(action.call_id, 2, "yes")
    claimed = await dialogue.claim(action.call_id, 2, "yes")
    assert await dialogue.authorize(
        action.call_id, claimed.state.version, claimed.state.input_epoch, 2
    )
    return Approval(
        call_id=action.call_id,
        action_id=action.action_id,
        revision=action.revision,
        turn_id=2,
        is_final=True,
        permitted=True,
        guard_version=claimed.state.version,
        input_epoch=claimed.state.input_epoch,
    )


async def test_move_reserves_slot_and_specialty_and_blocks_both_cancellations(move_stores):
    dialogue, booking, moving, cancelling, clock, before, slot, old = move_stores
    action = await moving.prepare("move-call", before, slot, clock.now())
    assert not await booking.available(slot, clock.now())
    with pytest.raises(BookingError, match="ACTION_PENDING"):
        await booking.hold(
            "rival",
            1,
            slot.model_copy(update={"scheduled_at": slot.scheduled_at + timedelta(days=1)}),
            clock.now(),
        )
    with pytest.raises(BookingError, match="ACTION_PENDING"):
        await cancelling.prepare("rival", before, clock.now())
    approval = await permitted(move_stores, action)
    await moving.claim(approval, clock.now())
    new = before.model_copy(update={"id": 2, "scheduled_at": slot.scheduled_at})
    await moving.created(action.action_id, new)
    with pytest.raises(BookingError, match="ACTION_PENDING"):
        await cancelling.prepare("rival", new, clock.now())
    await dialogue.end(action.call_id)
    await moving.release_call(action.call_id)
    assert (await moving.get(action.action_id)).state == MoveState.NEW_CREATED
    assert (await booking.get(old.action_id)).state == ActionState.BOOKED


async def test_one_actor_can_reserve_source_and_expiry_releases_only_new_slot(move_stores):
    _, booking, moving, _, clock, before, slot, old = move_stores
    results = await asyncio.gather(
        moving.prepare("a", before, slot, clock.now()),
        moving.prepare("b", before, slot, clock.now()),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, BookingError) for r in results) == 1
    action = next(r for r in results if not isinstance(r, Exception))
    clock.value += timedelta(seconds=90)
    assert await booking.available(slot, clock.now())
    assert (await moving.get(action.action_id)).state == MoveState.EXPIRED
    assert (await booking.get(old.action_id)).state == ActionState.BOOKED


@pytest.mark.parametrize("change", ["partial", "patient", "old_visit", "destination", "end"])
async def test_saved_confirmation_is_bound_to_patient_and_both_visits(move_stores, change):
    dialogue, _, moving, _, clock, before, slot, _ = move_stores
    action = await moving.prepare("move-call", before, slot, clock.now())
    approval = await permitted(move_stores, action)
    if change == "partial":
        await dialogue.observe(action.call_id, 3, "other")
    elif change == "end":
        await dialogue.end(action.call_id)
    else:
        state = await dialogue.inspect(action.call_id)
        if change == "patient":
            state = state.model_copy(update={"client_id": 2})
        elif change == "old_visit":
            shown = state.move_proposal.model_copy(
                update={"before": state.move_proposal.before.model_copy(update={"visit_id": 2})}
            )
            state = state.model_copy(update={"move_proposal": shown})
        else:
            shown = state.move_proposal.model_copy(
                update={
                    "slot": slot.model_copy(
                        update={"scheduled_at": slot.scheduled_at + timedelta(hours=1)}
                    )
                }
            )
            state = state.model_copy(update={"move_proposal": shown})
        if isinstance(dialogue, MemoryDialogueStore):
            dialogue.states[action.call_id] = state
        else:
            async with dialogue.engine.begin() as connection:
                await connection.execute(
                    update(dialogue_sessions)
                    .where(dialogue_sessions.c.call_id == action.call_id)
                    .values(state=state.model_dump(mode="json"))
                )
    with pytest.raises(BookingError, match="CONFIRMATION_INTERRUPTED"):
        await moving.claim(approval, clock.now())
    assert (await moving.get(action.action_id)).state == MoveState.HELD


async def test_completed_move_archives_old_registers_new_and_audits_exactly_once(move_stores):
    _, booking, moving, cancelling, clock, before, slot, old = move_stores
    action = await moving.prepare("move-call", before, slot, clock.now())
    approval = await permitted(move_stores, action)
    claims = await asyncio.gather(*(moving.claim(approval, clock.now()) for _ in range(5)))
    assert sum(won for _, won in claims) == 1
    new = before.model_copy(update={"id": 2, "scheduled_at": slot.scheduled_at})
    await moving.created(action.action_id, new)
    await moving.claim(approval, clock.now(), cancelling=True)
    cancelled = before.model_copy(update={"status": VisitStatus.CANCELLED})
    results = await asyncio.gather(
        *(moving.complete(action.action_id, cancelled, new, clock.now()) for _ in range(5))
    )
    assert all(r.state == MoveState.RESCHEDULED for r in results)
    assert (await booking.get(old.action_id)).state == ActionState.ARCHIVED
    assert (await booking.get(action.action_id)).visit_id == 2
    assert (await cancelling.get(action.action_id)).reason == "reschedule"
    assert not await booking.available(slot, clock.now())


async def test_unsubmitted_create_keeps_original_deadline(move_stores):
    _, _, moving, _, clock, before, slot, _ = move_stores
    action = await moving.prepare("move-call", before, slot, clock.now())
    approval = await permitted(move_stores, action)
    await moving.claim(approval, clock.now())
    restored = await moving.restore_unsent(action.action_id, approval.turn_id)
    assert restored.expires_at == action.expires_at and restored.state == MoveState.HELD
    assert restored.create_turn is None
