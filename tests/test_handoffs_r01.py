import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.api.handoffs import TextRequestInput
from app.clock import CLINIC_TIMEZONE
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import Phase, TransferRequest, Turn, reply
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.calendar import CalendarUnavailable, callback_deadline, is_open_day
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.models import CallbackDraft, MisDeliveryState
from app.handoffs.ports import HandoffConflict
from app.handoffs.service import HandoffService, HandoffSettings, parse_preferred_time
from app.mis.http import HttpMedicalSystem
from app.mis.models import Escalation, EscalationCreate
from app.mis.ports import MisError, MisOutcomeUnknown
from app.main import create_app
from app.settings import Settings


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


def test_callback_calendar_marks_2026_victory_day_and_transferred_day_closed():
    assert not is_open_day(datetime(2026, 5, 9).date())
    assert not is_open_day(datetime(2026, 5, 11).date())
    assert callback_deadline(
        datetime(2026, 5, 8, 20, 10, tzinfo=CLINIC_TIMEZONE), "P3"
    ) == datetime(2026, 5, 12, 10, tzinfo=CLINIC_TIMEZONE)


def test_callback_calendar_matches_all_configured_2026_nonworking_days():
    closed = {
        *(date(2026, 1, day) for day in range(1, 12)),
        date(2026, 2, 23),
        date(2026, 3, 9),
        date(2026, 5, 1),
        date(2026, 5, 9),
        date(2026, 5, 11),
        date(2026, 6, 12),
        date(2026, 11, 4),
        date(2026, 12, 31),
    }
    day = date(2026, 1, 1)
    while day <= date(2026, 12, 31):
        expected_open = day.weekday() < 6 and day not in closed
        assert is_open_day(day) is expected_open, day
        day += timedelta(days=1)


def test_callback_deadline_clinic_open_and_close_boundaries():
    def at(month, day, hour, minute):
        return datetime(2026, month, day, hour, minute, tzinfo=CLINIC_TIMEZONE)

    assert callback_deadline(at(10, 19, 7, 0), "P2") == at(10, 19, 7, 15)
    assert callback_deadline(at(10, 19, 19, 30), "P3") == at(10, 19, 20, 0)
    assert callback_deadline(at(10, 19, 19, 31), "P3") == at(10, 20, 10, 0)
    assert callback_deadline(at(10, 24, 8, 30), "P2") == at(10, 24, 8, 45)
    assert callback_deadline(at(10, 24, 14, 45), "P2") == at(10, 24, 15, 0)
    assert callback_deadline(at(10, 24, 14, 46), "P2") == at(10, 26, 10, 0)


def test_parse_preferred_time_converts_parser_iso_values():
    now = datetime(2026, 10, 19, 10, tzinfo=CLINIC_TIMEZONE)
    assert parse_preferred_time("завтра в 15:00", now) == datetime(
        2026, 10, 20, 15, tzinfo=CLINIC_TIMEZONE
    )


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
async def test_final_observation_has_its_own_watermark_and_is_idempotent():
    service, store, _, event, request = await prepared_callback()
    draft = await store.get_callback_draft(request.request_id)
    assert draft is not None

    await service.callback_final_observed(event.call_id, 5)
    observed = await store.get_callback_draft(request.request_id)
    assert observed is not None
    assert observed.last_turn_id == 4
    assert observed.started_turn_id == 0
    assert observed.observed_turn_id == 5
    assert observed.input_epoch == draft.input_epoch + 1

    # Поздняя доставка input_started для уже увиденного final не меняет CAS-эпоху.
    await service.callback_input_started(event.call_id, 5)
    repeated = await store.get_callback_draft(request.request_id)
    assert repeated == observed


@pytest.mark.asyncio
async def test_preferred_callback_time_after_sla_requires_explicit_confirmation():
    service, store, clock, event, request = await prepared_callback()
    clock.value = datetime(2026, 10, 19, 19, 50, tzinfo=CLINIC_TIMEZONE)
    # Recompute the offer after closing: the P3 deadline is tomorrow at 10:00.
    await store.delete_callback_draft(request.request_id)
    request = await store.get_request(request.request_id)
    await service.start_callback(event.call_id, 5, "no_answer")
    await service.callback_turn(event.call_id, Turn(turn_id=6, text="да"))
    await service.callback_turn(event.call_id, Turn(turn_id=7, text="79991234567"))

    result = await service.callback_turn(event.call_id, Turn(turn_id=8, text="завтра в 15:00"))
    draft = await store.get_callback_draft(request.request_id)

    assert draft is not None and draft.preferred_at == datetime(
        2026, 10, 20, 15, tzinfo=CLINIC_TIMEZONE
    )
    assert draft.deadline == datetime(2026, 10, 20, 10, tzinfo=CLINIC_TIMEZONE)
    assert any("позже норматива" in text for text in result.utterances)
    assert any("Подтвердите ожидание" in text for text in result.utterances)
    assert await store.get_callback_task_by_request(request.request_id) is None

    confirmed = await service.callback_turn(event.call_id, Turn(turn_id=9, text="да"))
    task = await store.get_callback_task_by_request(request.request_id)
    assert confirmed.code == "CALLBACK_QUEUED"
    assert task is not None and task.preferred_at == draft.preferred_at


@pytest.mark.asyncio
async def test_ambiguous_callback_time_repeats_deadline_without_queuing():
    service, store, _, event, request = await prepared_callback()
    await store.delete_callback_draft(request.request_id)
    await service.start_callback(event.call_id, 5, "no_answer")
    await service.callback_turn(event.call_id, Turn(turn_id=6, text="да"))
    await service.callback_turn(event.call_id, Turn(turn_id=7, text="79991234567"))
    draft = await store.get_callback_draft(request.request_id)
    assert draft is not None and draft.stage == "time"

    result = await service.callback_turn(
        event.call_id, Turn(turn_id=8, text="Когда вы сможете перезвонить?")
    )

    updated = await store.get_callback_draft(request.request_id)
    assert result.code == "CALLBACK_STEP"
    assert updated is not None and updated.stage == "time"
    assert updated.deadline == draft.deadline
    assert any("Нормативный срок" in utterance for utterance in result.utterances)
    assert await store.get_callback_task_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_http_replay_of_final_callback_turn_returns_the_same_task():
    service, handoff_store, clock, event, request = await prepared_callback()
    dialogue_store = MemoryDialogueStore()
    for turn_id in range(1, 5):
        claim = await dialogue_store.claim(event.call_id, turn_id, f"seed-{turn_id}")
        if turn_id == 4:
            await dialogue_store.preempt(
                event.call_id,
                turn_id,
                TransferRequest(route="R-08", priority="P3"),
            )
        else:
            await dialogue_store.complete(
                claim.state,
                turn_id,
                reply(claim.state, turn_id, "Продолжаем."),
            )
    dialogue = DialogueService(
        None,
        dialogue_store,
        b"x" * 32,
        safety=RulesSafety(clock),
        handoffs=service,
    )

    @asynccontextmanager
    async def dialogue_factory(settings, selected_clock):
        yield dialogue

    app = create_app(
        Settings(_env_file=None, backend_demo_enabled=True),
        clock=clock,
        dialogue_factory=dialogue_factory,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            payload = {"turn_id": 5, "text": "да"}
            first = await client.post(f"/api/dialog/{event.call_id}/turn", json=payload)
            replay = await client.post(f"/api/dialog/{event.call_id}/turn", json=payload)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["code"] == "CALLBACK_QUEUED"
    tasks = await handoff_store.list_callback_tasks(limit=50, offset=0)
    assert len(tasks) == 1 and tasks[0].request_id == request.request_id
    assert await handoff_store.get_callback_task_by_request(request.request_id) == tasks[0]


def test_callback_draft_without_observed_turn_is_backward_compatible():
    draft = CallbackDraft.model_validate(
        {
            "request_id": "a" * 32,
            "stage": "offer",
            "last_turn_id": 1,
            "deadline": "2026-10-19T10:30:00+04:00",
            "summary": "Вопрос пациента",
        }
    )
    assert draft.started_turn_id == 0
    assert draft.observed_turn_id == 0


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
    assert len(await store.list_callback_tasks(limit=50, offset=0)) == 1
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
@pytest.mark.parametrize(
    "response_client_id,response_description,accepted",
    [
        (42, "[T08:test] operator transfer", True),
        (43, "[T08:test] operator transfer", False),
        (42, "[T08:test] other content", False),
    ],
)
async def test_escalation_http_201_requires_matching_client_and_description(
    response_client_id, response_description, accepted
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            201,
            json={
                "id": 91,
                "client_id": response_client_id,
                "description": response_description,
                "created_at": "19.10.2026 10:00",
            },
        )

    description = "[T08:test] operator transfer"
    async with httpx.AsyncClient(
        base_url="https://mis.invalid/api/v1/", transport=httpx.MockTransport(respond)
    ) as client:
        system = HttpMedicalSystem(client, BudgetSpy())
        if accepted:
            result = await system.create_escalation(
                EscalationCreate(client_id=42, description=description)
            )
            assert result.id == 91 and result.client_id == 42
            assert result.description == description
        else:
            with pytest.raises(MisOutcomeUnknown, match="INVALID_RESPONSE"):
                await system.create_escalation(
                    EscalationCreate(client_id=42, description=description)
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
@pytest.mark.parametrize("mismatch", ["foreign_client", "duplicate_marker"])
async def test_uncertain_mis_reconciliation_never_applies_foreign_or_duplicate_marker(mismatch):
    class Mis:
        items = []

        async def escalations(self, client_id, *, limit, offset):
            return self.items

    mis = Mis()
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        mis,
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id=f"reconcile-{mismatch}",
        call_id=f"reconcile-call-{mismatch}",
        route="R-01",
        priority="P1",
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    request = await store.request_for_call(event.call_id)
    operation = await store.get_mis_operation(request.mis_operation_id)
    matching = Escalation(
        id=42,
        client_id=operation.client_id,
        description=operation.description,
        created_at="19.10.2026 10:00",
    )
    if mismatch == "foreign_client":
        mis.items = [matching.model_copy(update={"client_id": operation.client_id + 1})]
        expected_reason = "RECONCILE_MARKER_MISMATCH"
    else:
        mis.items = [matching, matching.model_copy(update={"id": 43})]
        expected_reason = "DUPLICATE_MARKER"

    await service._reconcile_mis(operation, clock.now())
    checked = await store.get_mis_operation(operation.operation_id)
    assert checked.state == MisDeliveryState.MANUAL
    assert checked.reason == expected_reason


@pytest.mark.asyncio
async def test_reconciliation_marks_paginated_result_unstable_when_new_item_shifts_pages():
    class Mis:
        def __init__(self):
            self.items = []
            self.offsets = []
            self.inserted = False

        async def escalations(self, client_id, *, limit, offset):
            self.offsets.append(offset)
            page = self.items[offset : offset + limit]
            if offset == 0 and not self.inserted:
                self.items.insert(
                    0,
                    Escalation(
                        id=999,
                        client_id=client_id,
                        description="new item inserted between pages",
                        created_at="19.10.2026 10:00",
                    ),
                )
                self.inserted = True
            return page

    mis = Mis()
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        mis,
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id="reconcile-page-shift",
        call_id="reconcile-page-shift-call",
        route="R-01",
        priority="P1",
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    request = await store.request_for_call(event.call_id)
    operation = await store.get_mis_operation(request.mis_operation_id)
    mis.items = [
        Escalation(
            id=index,
            client_id=operation.client_id,
            description=f"unrelated escalation {index}",
            created_at="19.10.2026 10:00",
        )
        for index in range(1, 101)
    ]
    mis.items.append(
        Escalation(
            id=101,
            client_id=operation.client_id,
            description=operation.description,
            created_at="19.10.2026 10:00",
        )
    )

    await service._reconcile_mis(operation, clock.now())

    checked = await store.get_mis_operation(operation.operation_id)
    assert mis.offsets == [0, 50, 100]
    assert checked.state == MisDeliveryState.MANUAL
    assert checked.reason == "RECONCILE_INCOMPLETE"
    assert checked.escalation_id is None


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
async def test_callback_exhausts_after_three_attempts_and_keeps_sms_pending():
    service, store, clock, event, request = await prepared_callback()
    await service.callback_turn(event.call_id, Turn(turn_id=5, text="да"))
    task = await store.get_callback_task_by_request(request.request_id)

    for index in range(3):
        owner = f"operator-{index}"
        claimed = await service.operator_claim(task.task_id, owner)
        task, attempt = await service.operator_start_attempt(task.task_id, owner, claimed.version)
        task = await service.operator_finish_attempt(
            attempt.attempt_id, "no_answer", owner=owner, version=task.version
        )
        if index < 2:
            clock.value += timedelta(minutes=5)

    assert task.attempts == 3
    assert task.state.value == "exhausted"
    assert task.sms_pending is True
    with pytest.raises(HandoffConflict):
        await service.operator_claim(task.task_id, "operator-four")


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
        HandoffSettings(enabled=True, callback_enabled=True, technical_client_id=999),
    )
    event = SimpleNamespace(
        attempt_id="urgent-1", call_id="urgent-call", route="R-01", priority="P1"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    assert await service.start_callback(event.call_id, 1, "no_answer") is None
    request = await store.request_for_call(event.call_id)
    assert request.mis_operation_id is not None
    assert len(store.mis_operations) == 1
    operation = next(iter(store.mis_operations.values()))
    assert operation.kind == "emergency_notice"
    assert operation.client_id == 999
    assert store.callback_drafts == {}
    assert store.callback_tasks == {}


@pytest.mark.asyncio
async def test_p1_without_technical_client_stays_manual_without_mis_lookup():
    store, clock = MemoryHandoffStore(), Clock()
    service = HandoffService(
        store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    event = SimpleNamespace(
        attempt_id="urgent-no-client", call_id="urgent-no-client-call", route="R-01", priority="P1"
    )

    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )

    request = await store.request_for_call(event.call_id)
    assert request is not None
    assert request.mis_state == MisDeliveryState.MANUAL
    assert request.mis_operation_id is None
    assert store.mis_operations == {}
    assert await service.start_callback(event.call_id, 1, "no_answer") is None
    assert store.callback_drafts == {}
    assert store.callback_tasks == {}


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
