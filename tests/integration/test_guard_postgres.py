"""Барьер и гонки проверяются в БД, а не только в одном Python-процессе."""

import asyncio

import pytest
from fixture_bridge import pipeline, turn

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError
from app.demo import DemoClock, DemoSchedule
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import Phase, Proposal, TransferRequest, reply
from app.storage.booking import PostgresBookingStore
from app.storage.dialogue import PostgresDialogueStore


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def stores(request):
    if request.param == "memory":
        dialogue = MemoryDialogueStore()
        return dialogue, MemoryBookingStore(guard_reader=dialogue)
    engine = request.getfixturevalue("pg_engine")
    return PostgresDialogueStore(engine), PostgresBookingStore(engine)


async def permitted(stores):
    dialogue, booking = stores
    clock = DemoClock()
    action = await booking.hold("call", 1, DemoSchedule(clock.now()).slots()[0], clock.now())
    claim = await dialogue.claim("call", 1, "initial")
    state = claim.state.model_copy(
        update={
            "phase": Phase.HELD,
            "proposal": Proposal(
                action_id=action.action_id,
                revision=action.revision,
                slot=action.slot,
                expires_at=action.expires_at,
            ),
        }
    )
    await dialogue.complete(state, 1, reply(state, 1, "Подтвердите выбранный приём."))
    await dialogue.observe("call", 2, "final-yes")
    claim = await dialogue.claim("call", 2, "final-yes")
    assert await dialogue.authorize("call", claim.state.version, claim.state.input_epoch, 2)
    approval = Approval(
        call_id="call",
        action_id=action.action_id,
        revision=action.revision,
        turn_id=2,
        is_final=True,
        permitted=True,
        guard_version=claim.state.version,
        input_epoch=claim.state.input_epoch,
    )
    return dialogue, booking, approval, clock


async def test_guard_claim_once_with_saved_server_stamp(stores):
    dialogue, booking, approval, clock = await permitted(stores)
    claims = await asyncio.gather(*(booking.claim(approval, clock.now()) for _ in range(8)))
    assert sum(winner for _, winner in claims) == 1


async def test_restore_unsent_checks_owner_and_preserves_original_hold(stores):
    dialogue, booking, approval, clock = await permitted(stores)
    original, won = await booking.claim(approval, clock.now())
    assert won
    with pytest.raises(BookingError, match="ACTION_NOT_UNSENT"):
        await booking.restore_unsent(approval.action_id, approval.turn_id + 1)
    restored = await booking.restore_unsent(approval.action_id, approval.turn_id)
    assert restored.state == "held" and restored.confirmed_turn is None
    assert restored.expires_at == original.expires_at
    assert restored.revision == original.revision
    await booking.claim(approval, clock.now())
    await booking.finish(approval.action_id, ActionState.RECONCILING)
    with pytest.raises(BookingError, match="ACTION_NOT_UNSENT"):
        await booking.restore_unsent(approval.action_id, approval.turn_id)


@pytest.mark.parametrize("change", ["partial", "new_final", "end", "handoff"])
async def test_saved_stamp_is_revoked_by_new_input_or_terminal_state(stores, change):
    dialogue, booking, approval, clock = await permitted(stores)
    if change in {"partial", "new_final"}:
        await dialogue.observe("call", 3, change)
    elif change == "end":
        await dialogue.end("call")
    else:
        await dialogue.preempt("call", 2, TransferRequest(route="R-01", priority="P1"))
    with pytest.raises(BookingError, match="CONFIRMATION_INTERRUPTED"):
        await booking.claim(approval, clock.now())
    assert (await booking.get(approval.action_id)).state == "held"


async def test_forged_stamp_cannot_authorize_write(stores):
    dialogue, booking, approval, clock = await permitted(stores)
    for field, value in (
        ("guard_version", 99),
        ("input_epoch", 99),
        ("revision", 99),
        ("turn_id", 99),
    ):
        with pytest.raises(BookingError):
            await booking.claim(approval.model_copy(update={field: value}), clock.now())


async def test_late_completion_cannot_restore_permission_or_handoff(stores):
    dialogue, booking, approval, clock = await permitted(stores)
    old = await dialogue.inspect("call")
    await dialogue.observe("call", 3, "new-input")
    assert not await dialogue.complete(old, 2, reply(old, 2, "Поздний ответ."))
    current = await dialogue.inspect("call")
    assert current.authorization is None and current.pending_turn_id is None
    replay = await dialogue.claim("call", 2, "final-yes")
    assert replay.cached.code == "TURN_INTERRUPTED"
    with pytest.raises(BookingError):
        await booking.claim(approval, clock.now())


@pytest.mark.postgres
async def test_postgres_pipeline_creates_one_visit_with_atomic_gate(pg_engine):
    async with pipeline(pg_engine=pg_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого")
        await turn(client, 3, "В десять тридцать")
        result = await turn(client, 4, "Да")
        assert result["code"] == "BOOKED" and emulator.write_count == 1
        assert await turn(client, 4, "Да") == result
        assert emulator.write_count == 1


@pytest.mark.postgres
async def test_postgres_partial_during_preflight_prevents_post(pg_engine):
    async with pipeline(pg_engine=pg_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого")
        await turn(client, 3, "В десять тридцать")
        ready, finish = asyncio.Event(), asyncio.Event()
        original = service.booking._preflight

        async def delayed(*args, **kwargs):
            ready.set()
            await finish.wait()
            return await original(*args, **kwargs)

        service.booking._preflight = delayed
        task = asyncio.create_task(turn(client, 4, "Да"))
        await ready.wait()
        assert (await turn(client, 5, "не могу дышать", final=False))["code"] == "EMERGENCY"
        finish.set()
        assert (await task)["code"] == "EMERGENCY"
        assert emulator.write_count == 0
