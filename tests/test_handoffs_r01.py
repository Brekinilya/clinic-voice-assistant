import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.api.handoffs import TextRequestInput
from app.clock import CLINIC_TIMEZONE
from app.dialogue.models import Turn
from app.handoffs.calendar import CalendarUnavailable, callback_deadline
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.models import MisDeliveryState
from app.handoffs.ports import HandoffConflict
from app.handoffs.service import HandoffService, HandoffSettings
from app.mis.http import HttpMedicalSystem
from app.mis.models import Escalation, EscalationCreate
from app.mis.ports import MisError, MisOutcomeUnknown


class BudgetSpy:
    async def reserve(self, cost=1):
        return None

    async def release(self, ids):
        pass

    async def defer(self, seconds):
        pass


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 19, 10, tzinfo=CLINIC_TIMEZONE)

    def now(self):
        return self.value


class NoMis:
    async def create_escalation(self, payload):
        raise AssertionError("MIS must be processed only by the bounded worker")


def test_callback_deadline_uses_saturday_sunday_holiday_and_clinic_hours():
    def at(year, month, day, hour, minute=0):
        return datetime(year, month, day, hour, minute, tzinfo=CLINIC_TIMEZONE)

    assert callback_deadline(at(2026, 10, 23, 20, 10), "P3") == at(2026, 10, 24, 10)
    assert callback_deadline(at(2026, 10, 25, 12), "P3") == at(2026, 10, 26, 10)
    assert callback_deadline(at(2026, 11, 3, 20, 10), "P3") == at(2026, 11, 5, 10)
    assert callback_deadline(at(2026, 10, 24, 14, 20), "P2") == at(2026, 10, 24, 14, 35)
    assert callback_deadline(at(2026, 10, 19, 19, 50), "P2") == at(2026, 10, 20, 10)


def test_callback_deadline_refuses_dates_outside_verified_coverage():
    with pytest.raises(CalendarUnavailable):
        callback_deadline(datetime(2027, 1, 1, 10, tzinfo=CLINIC_TIMEZONE), "P3")


async def prepared_callback(*, priority="P3", technical_client_id=None):
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(
            enabled=True, callback_enabled=True, technical_client_id=technical_client_id
        ),
    )
    event = SimpleNamespace(
        attempt_id="attempt-1", call_id="call-1", route="R-08", priority=priority
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await service.start_callback(event.call_id, 1, "no_answer")
    await service.callback_turn(event.call_id, Turn(turn_id=2, text="да"))
    await service.callback_turn(event.call_id, Turn(turn_id=3, text="79991234567"))
    await service.callback_turn(event.call_id, Turn(turn_id=4, text="в ближайшее время"))
    request = await store.request_for_call(event.call_id)
    return service, store, clock, event, request


@pytest.mark.asyncio
async def test_callback_consent_is_revoked_if_new_input_wins_insert_race(monkeypatch):
    service, store, _, event, request = await prepared_callback()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(service.callback_turn(event.call_id, Turn(turn_id=5, text="да")))
    await entered.wait()
    await service.callback_input_started(event.call_id, 6)
    release.set()
    result = await writer
    assert result.code == "CALLBACK_STALE"
    assert await store.get_callback_task_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_end_revokes_callback_before_insert(monkeypatch):
    service, store, _, event, request = await prepared_callback()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(service.callback_turn(event.call_id, Turn(turn_id=5, text="да")))
    await entered.wait()
    await service.discard_draft(event.call_id)
    release.set()
    result = await writer
    assert result.code == "CALLBACK_STALE"
    assert await store.get_callback_task_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_same_final_consent_racing_twice_creates_one_task(monkeypatch):
    service, store, _, event, request = await prepared_callback()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    first = asyncio.create_task(service.callback_turn(event.call_id, Turn(turn_id=5, text="да")))
    await entered.wait()
    second = asyncio.create_task(service.callback_turn(event.call_id, Turn(turn_id=5, text="да")))
    release.set()
    first_reply, second_reply = await asyncio.gather(first, second)
    task = await store.get_callback_task_by_request(request.request_id)
    assert task is not None
    assert len(await store.list_callback_tasks()) == 1
    assert {first_reply.code, second_reply.code} <= {"CALLBACK_QUEUED", "CALLBACK_STALE"}


@pytest.mark.asyncio
async def test_three_unrecognized_answers_do_not_save_callback_task():
    service, store, _, event, request = await prepared_callback()
    for turn_id in range(5, 8):
        reply = await service.callback_turn(
            event.call_id, Turn(turn_id=turn_id, text="непонятный ответ")
        )
    assert reply.code == "CALLBACK_UNDERSTANDING_LIMIT"
    assert await store.get_callback_task_by_request(request.request_id) is None


def test_text_request_route_is_a_fixed_route_code():
    assert TextRequestInput(route="R-08", priority="P3", summary="Вопрос").route == "R-08"
    with pytest.raises(ValidationError):
        TextRequestInput(route="<script>", priority="P3", summary="Вопрос")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(429, MisError), (500, MisOutcomeUnknown), (201, MisOutcomeUnknown)],
)
async def test_escalation_http_faults_are_single_shot_and_classified(status, expected):
    calls = []

    def respond(request):
        calls.append(request)
        if status == 201:
            return httpx.Response(201, json={"id": 9})  # invalid contract response
        return httpx.Response(status, json={"message": "private patient detail"})

    async with httpx.AsyncClient(
        base_url="https://mis.invalid/api/v1/", transport=httpx.MockTransport(respond)
    ) as client:
        system = HttpMedicalSystem(client, BudgetSpy())
        with pytest.raises(expected):
            await system.create_escalation(
                EscalationCreate(client_id=1, description="[T08:test] transfer")
            )
    assert len(calls) == 1
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/escalations"


@pytest.mark.asyncio
async def test_uncertain_mis_reconciliation_with_marker_mismatch_is_manual():
    class Mis:
        async def escalations(self, client_id, *, limit, offset):
            return [
                Escalation(
                    id=42,
                    client_id=client_id,
                    description=f"[{operation.marker}] different immutable text",
                    created_at="19.10.2026 10:00",
                )
            ]

    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        Mis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id="reconcile-1", call_id="reconcile-call", route="R-01", priority="P1"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    request = await store.request_for_call(event.call_id)
    operation = await store.get_mis_operation(request.mis_operation_id)
    await service._reconcile_mis(operation, clock.now())
    checked = await store.get_mis_operation(operation.operation_id)
    assert checked.state == MisDeliveryState.MANUAL
    assert checked.reason == "RECONCILE_MARKER_MISMATCH"


@pytest.mark.asyncio
async def test_expired_lease_cannot_start_and_recovery_marks_attempt_manual():
    service, store, clock, event, request = await prepared_callback()
    await service.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
    task = await store.get_callback_task_by_request(request.request_id)
    claimed = await service.operator_claim(task.task_id, "operator-a")
    clock.value += timedelta(minutes=11)
    with pytest.raises(HandoffConflict):
        await service.operator_start_attempt(task.task_id, "operator-a", claimed.version)
    recovered = await service.recover_callbacks_once()
    assert recovered[0].state.value == "queued"
    claimed = await service.operator_claim(task.task_id, "operator-a")
    task, attempt = await service.operator_start_attempt(
        task.task_id, "operator-a", claimed.version
    )
    clock.value += timedelta(minutes=11)
    recovered = await service.recover_callbacks_once()
    assert recovered[0].state.value == "manual"
    with pytest.raises(HandoffConflict):
        await service.operator_finish_attempt(
            attempt.attempt_id, "connected", owner="operator-a", version=task.version
        )


@pytest.mark.asyncio
async def test_text_request_is_not_a_connected_transfer():
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(store, NoMis(), clock, b"x" * 32, HandoffSettings(enabled=True))
    request = await service.create_text_request("R-08", "P3", "Вопрос по записи")
    assert request.source == "text"
    assert request.identity_status == "unknown"
    assert request.transfer_state.value == "requested"
    assert request.attempt_id is None


@pytest.mark.asyncio
async def test_emergency_intent_is_committed_with_transfer_result():
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id="urgent-1", call_id="urgent-call", route="R-01", priority="P1"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    request = await store.request_for_call(event.call_id)
    assert request.mis_operation_id is not None
    assert len(store.mis_operations) == 1
    operation = next(iter(store.mis_operations.values()))
    assert operation.kind == "emergency_notice"
    assert operation.client_id == 999


@pytest.mark.asyncio
async def test_result_from_another_route_cannot_create_mis_operation():
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id="urgent-2", call_id="urgent-call-2", route="R-01", priority="P1"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**{**vars(event), "route": "R-02"}, outcome="connected", reason=None)
    )
    request = await store.request_for_call(event.call_id)
    assert request.transfer_state.value == "attempting"
    assert request.mis_operation_id is None


@pytest.mark.asyncio
async def test_cancel_cannot_change_terminal_callback_result():
    service, store, _, event, request = await prepared_callback()
    await service.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
    task = await store.get_callback_task_by_request(request.request_id)
    claimed = await service.operator_claim(task.task_id, "operator-a")
    task, attempt = await service.operator_start_attempt(
        task.task_id, "operator-a", claimed.version
    )
    finished = await service.operator_finish_attempt(
        attempt.attempt_id, "connected", owner="operator-a", version=task.version
    )
    assert finished.state.value == "done"
    with pytest.raises(HandoffConflict):
        await service.operator_cancel(task.task_id, "operator-a", finished.version)
