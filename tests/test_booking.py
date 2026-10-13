"""Поведение сценария через настоящий HTTP-адаптер и автономную МИС."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError, Slot
from app.booking.service import BookingService
from app.demo import DemoClock, DemoSchedule, DemoSession, EmulatorBudget, demo_emulator
from app.mis.budget import MemoryBudget
from app.mis.http import HttpMedicalSystem
from app.mis.models import VisitCreate, parse_date


@pytest.fixture
async def booking():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        service = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()),
            MemoryBookingStore(),
            DemoSchedule(clock.now()),
            clock,
        )
        yield service, emulator, clock


def approval(action, **updates):
    return Approval(
        call_id=action.call_id,
        action_id=action.action_id,
        revision=action.revision,
        turn_id=5,
        is_final=True,
        permitted=True,
    ).model_copy(update=updates)


async def test_identification_is_exact_and_no_patient_is_created(booking):
    service, emulator, _ = booking
    result = await service.identify("Тестов", "Александр", "Примерович", parse_date("01.01.1990"))
    assert result.id == 1
    for last, middle in (("тестов", "Примерович"), ("Тестов", "Другое"), ("Тестов", None)):
        with pytest.raises(BookingError, match="IDENTIFICATION_REQUIRED"):
            await service.identify(last, "Александр", middle, parse_date("01.01.1990"))
    assert len(emulator.clients["demo"]) == 2 and emulator.write_count == 0


async def test_wrong_search_response_cannot_identify_patient(booking):
    service, emulator, _ = booking

    async def broken_search(*args):
        return [emulator.clients["demo"][2]]

    service.mis.clients = broken_search
    with pytest.raises(BookingError, match="IDENTIFICATION_REQUIRED"):
        await service.identify("Тестов", "Александр", "Примерович", parse_date("01.01.1990"))
    assert emulator.write_count == 0


async def test_three_choices_and_successful_confirmation_is_idempotent(booking):
    service, emulator, _ = booking
    options = await service.options(1, "терапевт")
    assert len(options) == 3
    action = await service.propose("call-1", 1, options[0])
    result = await service.confirm(approval(action))
    assert result.state == ActionState.BOOKED and result.visit_id == 1
    assert await service.confirm(approval(action)) == result
    assert emulator.write_count == 1


async def test_simple_full_scenario_fits_eight_request_budget(booking):
    service, emulator, clock = booking
    service.mis._budget = MemoryBudget(clock)
    patient = await service.identify("Тестов", "Александр", "Примерович", parse_date("01.01.1990"))
    choices = await service.options(patient.id, "терапевт")
    action = await service.propose("call-1", patient.id, choices[0])
    assert (await service.confirm(approval(action))).state == ActionState.BOOKED
    assert emulator.write_count == 1


@pytest.mark.parametrize(
    "updates", [{"is_final": False}, {"permitted": False}, {"revision": 2}, {"call_id": "other"}]
)
async def test_incomplete_or_stale_permission_never_writes(booking, updates):
    service, emulator, _ = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    with pytest.raises(BookingError, match="CONFIRMATION_REQUIRED"):
        await service.confirm(approval(action, **updates))
    assert emulator.write_count == 0


async def test_change_choice_invalidates_old_confirmation(booking):
    service, emulator, _ = booking
    first = await service.propose("call-1", 1, service.schedule.slots()[0])
    second = await service.propose("call-1", 1, service.schedule.slots()[1])
    assert second.revision == first.revision + 1
    with pytest.raises(BookingError, match="HOLD_NOT_ACTIVE"):
        await service.confirm(approval(first))
    assert emulator.write_count == 0
    assert (await service.confirm(approval(second))).state == ActionState.BOOKED


async def test_expiry_and_cancel_do_not_write(booking):
    service, emulator, clock = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    clock.value += timedelta(seconds=90)
    with pytest.raises(BookingError, match="HOLD_NOT_ACTIVE"):
        await service.confirm(approval(action))
    second = await service.propose("call-2", 2, service.schedule.slots()[0])
    await service.store.cancel(second.action_id, second.call_id)
    with pytest.raises(BookingError, match="HOLD_NOT_ACTIVE"):
        await service.confirm(approval(second))
    assert emulator.write_count == 0


async def test_simultaneous_slot_selection_has_one_winner(booking):
    service, emulator, _ = booking
    results = await asyncio.gather(
        service.propose("call-1", 1, service.schedule.slots()[0]),
        service.propose("call-2", 2, service.schedule.slots()[0]),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, BookingError) for r in results) == 1
    assert emulator.write_count == 0


async def test_simultaneous_confirmations_have_one_post(booking):
    service, emulator, _ = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    results = await asyncio.gather(*(service.confirm(approval(action)) for _ in range(8)))
    assert all(r.state in {ActionState.BOOKED, ActionState.COMMITTING} for r in results)
    assert emulator.write_count == 1


@pytest.mark.parametrize("failure", ["lost_response", "server_error", "invalid_response"])
async def test_unknown_write_stays_pending_until_readback(booking, failure):
    service, emulator, clock = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    emulator.next_failure = failure
    assert (await service.confirm(approval(action))).state == ActionState.RECONCILING
    clock.value += timedelta(minutes=5)
    assert not await service.store.available(action.slot, clock.now())
    assert (await service.confirm(approval(action))).state == ActionState.RECONCILING
    assert emulator.write_count == 1
    assert (await service.reconcile(action.action_id)).state == ActionState.BOOKED
    assert emulator.write_count == 1


async def test_empty_or_multiple_readback_cannot_claim_success(booking):
    service, emulator, clock = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    await service.store.claim(approval(action), clock.now())
    assert (await service.reconcile(action.action_id)).state == ActionState.RECONCILING
    request = VisitCreate(client_id=1, doctor_id=1, scheduled_at=action.slot.scheduled_at)
    await service.mis.create_visit(request)
    await service.mis.create_visit(request)
    assert (await service.reconcile(action.action_id)).state == ActionState.RECONCILING
    assert emulator.write_count == 2


async def test_process_interruption_leaves_journal_before_post(booking):
    service, emulator, _ = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])

    async def fail(request):
        assert (await service.store.get(action.action_id)).state == ActionState.COMMITTING
        raise asyncio.CancelledError()

    service.mis.create_visit = fail
    with pytest.raises(asyncio.CancelledError):
        await service.confirm(approval(action))
    assert (await service.store.get(action.action_id)).state == ActionState.COMMITTING
    assert (await service.confirm(approval(action))).state == ActionState.COMMITTING
    assert emulator.write_count == 0


async def test_unknown_eligibility_and_invalid_time_are_blocked(booking):
    service, emulator, clock = booking
    with pytest.raises(BookingError, match="ELIGIBILITY_UNKNOWN"):
        await service.propose("call-1", 99, service.schedule.slots()[0])
    for minutes in (29, 14 * 24 * 60):
        slot = Slot(
            doctor_id=1, specialty="терапевт", scheduled_at=clock.now() + timedelta(minutes=minutes)
        )

        class Schedule:
            def slots(self):
                return (slot,)

            def eligible(self, client_id, value):
                return True

        service.schedule = Schedule()
        with pytest.raises(BookingError, match="TOO_SOON|OUTSIDE_HORIZON"):
            await service.propose("call-1", 1, slot)
    assert emulator.write_count == 0


async def test_inactive_doctor_and_existing_specialty_are_rechecked(booking):
    service, emulator, _ = booking
    action = await service.propose("call-1", 1, service.schedule.slots()[0])
    emulator.doctors[1] = emulator.doctors[1].model_copy(update={"is_active": False})
    with pytest.raises(BookingError, match="DOCTOR_UNAVAILABLE"):
        await service.confirm(approval(action))
    assert emulator.write_count == 0
    emulator.doctors[1] = emulator.doctors[1].model_copy(update={"is_active": True})
    await service.mis.create_visit(
        VisitCreate(client_id=1, doctor_id=1, scheduled_at=service.schedule.slots()[1].scheduled_at)
    )
    with pytest.raises(BookingError, match="SPECIALTY_ALREADY_BOOKED"):
        await service.confirm(approval(action))
    assert emulator.write_count == 1


async def test_text_demo_unknown_input_and_lost_response(booking):
    service, emulator, _ = booking
    session = DemoSession(service, emulator)
    await session.turn_text("пациент Тестов; Александр; Примерович; 01.01.1990")
    await session.turn_text("варианты терапевт")
    await session.turn_text("выбрать 1")
    await session.turn_text("потерять-ответ")
    assert "не распознана" in (await session.turn_text("у меня болит живот"))[0]
    assert emulator.write_count == 0
    command = f"да {session.action.action_id} {session.action.revision}"
    assert "уточняется" in (await session.turn_text(command))[0]
    await session.turn_text(command)
    assert "подтверждена" in (await session.turn_text("сверить"))[0]
    assert emulator.write_count == 1
