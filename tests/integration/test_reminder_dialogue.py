"""Настоящий диалог С-10; только задания/отметки заменены контрактным адаптером.

Отмена и перенос идут через настоящие доменные сервисы и HTTP-эмулятор МИС.
Это не приёмка PostgreSQL-хранилища заданий Ильи или живого исходящего SIP.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fixture_bridge import pipeline, turn
from test_rescheduling import SETUP

from app.cancellation.models import summary
from app.dialogue.models import DialogueError, Phase, Turn
from app.dialogue.outbound import OutboundContext, OutboundEvent
from app.dialogue.reminder import ReminderConversation
from app.mis.models import VisitStatus
from app.recovery.models import OwnershipLost


class Actions:
    """Граница будущего адаптера Ильи; не выдаётся за реальный PATCH комментария."""

    def __init__(self, service):
        self.service = service
        self.valid = True
        self.status = "applied"
        self.calls = []
        self.gate = None
        self.entered = asyncio.Event()

    async def check_attempt(self, binding):
        if not self.valid or binding.fence != 1:
            raise OwnershipLost()

    async def _mark(self, binding, approval, operation):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        await self.check_attempt(binding)
        await self.service.store.check_reminder_guard(approval, binding, operation)
        self.calls.append((operation, binding.visit.visit_id))
        return self.status

    async def confirm(self, binding, approval):
        return await self._mark(binding, approval, "confirm")

    async def stop_calls(self, binding):
        self.calls.append(("stop_calls", binding.visit.visit_id))

    async def opt_out(self, binding, approval):
        return await self._mark(binding, approval, "opt_out")


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def reminder_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@asynccontextmanager
async def outgoing(engine=None, *, setup=None):
    async with pipeline(setup or SETUP, pg_engine=engine) as parts:
        client, service, emulator, *_ = parts
        actions = Actions(service)
        service.reminders = ReminderConversation(service.conversation, actions)
        visit = emulator.records["demo"][1]
        context = OutboundContext(
            attempt_id="attempt-1",
            fence=1,
            client_id=visit.client_id,
            visit=summary(visit),
            addressee="Ольга Викторовна",
        )
        yield client, service, emulator, actions, context


async def opened(service, context, *, verify=True):
    result = await service.open_outbound("scenario", context)
    if verify:
        result = await service.handle("scenario", Turn(turn_id=2, text="Да"))
    return result


async def test_open_is_scoped_idempotent_and_does_not_persist_addressee(reminder_engine):
    async with outgoing(reminder_engine) as (_, service, emulator, actions, context):
        first = await opened(service, context, verify=False)
        assert first.phase == Phase.REMIND_VERIFY and first.question.key == "reminder_addressee"
        assert first == await service.open_outbound("scenario", context)
        state = await service.store.inspect("scenario")
        assert state.client_id is None
        assert "Ольга" not in state.model_dump_json()
        assert "терапевт" not in " ".join(first.utterances)
        with pytest.raises(DialogueError, match="TURN_CONTENT_CONFLICT"):
            await service.open_outbound("scenario", context.model_copy(update={"fence": 2}))
        assert not actions.calls and emulator.patch_count == emulator.write_count == 0


async def test_open_cannot_replace_an_existing_inbound_conversation(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, _, _, context):
        await turn(client, 1, "Хочу записаться к терапевту")
        before = await service.store.inspect("scenario")
        with pytest.raises(DialogueError):
            await service.open_outbound("scenario", context)
        assert await service.store.inspect("scenario") == before


@pytest.mark.parametrize("answer", ["Переносите", "Отменяйте", "Да, приду", "Может быть", "Слушаю"])
async def test_only_unambiguous_addressee_answer_reveals_details(reminder_engine, answer):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=False)
        result = await turn(client, 2, answer)
        assert result["phase"] == "remind_verify"
        assert "терапевт" not in " ".join(result["utterances"])
        assert not actions.calls and emulator.patch_count == emulator.write_count == 0


async def test_third_party_during_reminder_is_transferred_without_disclosure(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=False)
        result = await turn(client, 2, "Это не для меня, а для мамы")
        assert result["code"] == "TRANSFER_REQUESTED"
        assert result["transfer"]["route"] == "R-16"
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert "терапевт" not in " ".join(result["utterances"])
        assert not actions.calls and emulator.patch_count == emulator.write_count == 0
        state = await service.store.inspect("scenario")
        assert state.phase == Phase.HANDOFF and state.client_id is None


async def test_first_yes_only_verifies_then_final_yes_calls_marks_once(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        details = await opened(service, context)
        assert details.question.key == "reminder_attendance"
        assert not actions.calls
        result = await turn(client, 3, "Да, приду")
        assert result["phase"] == "ended"
        assert result["reminder_result"]["outcome"] == "CONFIRMED"
        assert result["reminder_result"]["mis_status"] == "applied"
        assert result == await turn(client, 3, "Да, приду")
        assert actions.calls == [("confirm", 1)]
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize(
    "verify,utterance,expected_phase,expected_code",
    [
        (False, "Подождите минутку.", "remind_verify", "REMINDER_WAITING"),
        (True, "Подождите минутку.", "remind_details", "REMINDER_WAITING"),
        (False, "Спасибо, до свидания.", "ended", "MAYBE"),
        (True, "Спасибо, до свидания.", "ended", "MAYBE"),
    ],
)
async def test_wait_and_goodbye_do_not_mark_mis(
    reminder_engine, verify, utterance, expected_phase, expected_code
):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=verify)
        result = await turn(client, 3 if verify else 2, utterance)

        assert result["phase"] == expected_phase
        assert result["code"] == expected_code
        assert not actions.calls
        assert emulator.write_count == emulator.patch_count == 0
        if expected_phase == "ended":
            assert result["reminder_result"]["outcome"] == "MAYBE"
            assert result["reminder_result"]["mis_status"] == "not_required"


@pytest.mark.parametrize(
    "answer", ["Нет, не звоните", "Не надо мне больше звонить", "Отключите обзвон"]
)
async def test_opt_out_before_identity_blocks_contact_but_cannot_patch_client(
    reminder_engine, answer
):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=False)
        result = await turn(client, 2, answer)
        assert result["reminder_result"]["outcome"] == "OPT_OUT"
        assert result["reminder_result"]["mis_status"] == "manual"
        assert actions.calls == [("stop_calls", 1)]
        assert emulator.write_count == emulator.patch_count == 0


async def test_repeated_ambiguous_decline_cannot_loop_forever(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        await turn(client, 3, "Нет")
        for number in (4, 5, 6):
            result = await turn(client, number, "Нет")
        assert result["transfer"]["route"] == "R-05"
        assert not actions.calls and emulator.write_count == emulator.patch_count == 0


async def test_simple_attendance_answer_does_not_fetch_doctors(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, _, _, context):
        await opened(service, context)
        requests = []

        async def capture(request):
            requests.append(request.url.path)

        service.booking.mis._client.event_hooks["request"].append(capture)
        await turn(client, 3, "Да, приду")
        assert requests == []


async def test_lease_is_rechecked_after_cancel_preflight_before_patch(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        await turn(client, 3, "Отмените запись")
        await turn(client, 4, "Отменить")

        async def revoke(request):
            if request.method == "GET" and request.url.path.endswith("/visits/1"):
                actions.valid = False

        service.booking.mis._client.event_hooks["request"].append(revoke)
        result = await turn(client, 5, "Да")
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert emulator.write_count == emulator.patch_count == 0


async def test_lost_lease_after_new_visit_prevents_old_visit_patch(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        await turn(client, 3, "Перенесите на следующую неделю")
        await turn(client, 4, "Вторник")

        async def revoke(request):
            if request.method == "GET" and request.url.path.endswith("/visits/2"):
                actions.valid = False

        service.booking.mis._client.event_hooks["request"].append(revoke)
        result = await turn(client, 5, "Да")
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert emulator.write_count == 1 and emulator.patch_count == 0
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.records["demo"][2].status == VisitStatus.PLANNED
        # Уже созданная запись остаётся в журнале; автоматического повторного POST нет.
        await turn(client, 6, "Да")
        assert emulator.write_count == 1 and emulator.patch_count == 0


async def test_unknown_cancel_is_reconciled_on_repeat_without_second_patch(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        await turn(client, 3, "Отмените запись")
        await turn(client, 4, "Отменить")
        emulator.next_failure = "lost_response"
        result = await turn(client, 5, "Да")
        assert result["code"] == "CANCEL_RECONCILING"
        assert emulator.patch_count == 1
        result = await turn(client, 6, "Повторите")
        assert result["reminder_result"]["outcome"] == "CANCELLED"
        assert emulator.patch_count == 1


async def test_long_attempt_id_can_still_authorize_a_mark(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, _, actions, context):
        context = context.model_copy(update={"attempt_id": "a" * 128})
        await opened(service, context)
        result = await turn(client, 3, "Да")
        assert result["reminder_result"]["outcome"] == "CONFIRMED"
        assert len(actions.calls) == 1


async def test_one_move_option_yes_selects_it_but_does_not_write(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        service.booking.schedule._slots = tuple(
            slot
            for slot in service.booking.schedule.slots()
            if slot.doctor_id == context.visit.doctor_id and slot.scheduled_at.day == 6
        )
        await opened(service, context)
        result = await turn(client, 3, "Перенесите на следующую неделю")
        assert len(result["options"]) == 1
        result = await turn(client, 4, "Да")
        assert result["code"] == "MOVE_CONFIRMATION_REQUIRED"
        assert emulator.write_count == emulator.patch_count == 0
        result = await turn(client, 5, "Да")
        assert result["reminder_result"]["outcome"] == "RESCHEDULED"


async def test_no_with_new_date_refines_move_instead_of_ending_call(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        await turn(client, 3, "Перенесите на следующую неделю")
        await turn(client, 4, "Вторник")
        result = await turn(client, 5, "Нет, лучше в среду")
        assert result["code"] == "MOVE_OPTIONS", result
        assert all(item["scheduled_at"].startswith("2026-10-07") for item in result["options"])
        assert emulator.write_count == emulator.patch_count == 0


async def test_patient_can_switch_from_cancel_offer_to_move(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        await turn(client, 3, "Отмените запись")
        result = await turn(client, 4, "Не отменяйте, перенесите на следующую неделю")
        assert result["code"] == "MOVE_OPTIONS", result
        assert result["cancel_proposal"] is None
        assert emulator.write_count == emulator.patch_count == 0


async def test_lost_move_response_requires_readback_and_new_confirmation(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        await turn(client, 3, "Перенесите на следующую неделю")
        await turn(client, 4, "Вторник")
        emulator.next_failure = "lost_response"
        result = await turn(client, 5, "Да")
        assert result["code"] == "MOVE_PENDING"
        assert emulator.write_count == 1 and emulator.patch_count == 0
        result = await turn(client, 6, "Повторите")
        assert result["code"] == "MOVE_CONFIRMATION_REQUIRED"
        assert emulator.write_count == 1 and emulator.patch_count == 0
        result = await turn(client, 7, "Да")
        assert result["reminder_result"]["outcome"] == "RESCHEDULED"
        assert emulator.write_count == emulator.patch_count == 1


@pytest.mark.parametrize("status", ["pending", "manual"])
async def test_unverified_mark_is_not_reported_as_applied(reminder_engine, status):
    async with outgoing(reminder_engine) as (client, service, _, actions, context):
        await opened(service, context)
        actions.status = status
        result = await turn(client, 3, "Да")
        assert result["reminder_result"]["mis_status"] == status
        assert "требует проверки" in " ".join(result["utterances"])
        await turn(client, 4, "Да")
        assert len(actions.calls) == 1


@pytest.mark.parametrize(
    "answer,outcome", [("Нет, это её муж", "WRONG_PERSON"), ("Не знаю, может быть", "MAYBE")]
)
async def test_wrong_person_or_maybe_does_not_mutate(reminder_engine, answer, outcome):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=outcome != "WRONG_PERSON")
        result = await turn(client, 3 if outcome == "MAYBE" else 2, answer)
        assert result["reminder_result"]["outcome"] == outcome
        assert not actions.calls and emulator.patch_count == emulator.write_count == 0


async def test_bare_no_or_yes_after_decline_never_cancels(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        result = await turn(client, 3, "Не смогу прийти")
        assert result["code"] == "REMINDER_DECLINED"
        assert result["question"]["key"] == "reminder_cancel_or_move"
        result = await turn(client, 4, "Да")
        assert result["code"] == "NOT_UNDERSTOOD"
        assert not actions.calls and emulator.patch_count == emulator.write_count == 0


async def test_cancel_uses_existing_service_and_requires_final_confirmation(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        offered = await turn(client, 3, "Отмените запись")
        assert offered["code"] == "RESCHEDULE_OFFERED"
        prepared = await turn(client, 4, "Отменить")
        assert prepared["code"] == "CANCEL_CONFIRMATION_REQUIRED"
        assert emulator.patch_count == 0
        result = await turn(client, 5, "Да")
        assert result["reminder_result"] == {
            "attempt_id": "attempt-1",
            "outcome": "CANCELLED",
            "mis_status": "applied",
        }
        assert emulator.records["demo"][1].status == VisitStatus.CANCELLED
        assert emulator.patch_count == 1 and emulator.write_count == 0


async def test_cancel_refusal_returns_to_reminder_without_changing_visit(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        await turn(client, 3, "Отмените запись")
        await turn(client, 4, "Отменить")
        result = await turn(client, 5, "Нет")
        assert result["phase"] == "remind_details" and result["cancel_proposal"] is None
        assert emulator.patch_count == emulator.write_count == 0


async def test_reschedule_reuses_real_services_and_preserves_old_until_final_yes(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, _, context):
        await opened(service, context)
        result = await turn(client, 3, "Перенесите на следующую неделю")
        assert result["code"] == "MOVE_OPTIONS", result
        result = await turn(client, 4, "Вторник")
        assert result["code"] == "MOVE_CONFIRMATION_REQUIRED", result
        assert emulator.write_count == emulator.patch_count == 0
        result = await turn(client, 5, "Да")
        assert result["reminder_result"]["outcome"] == "RESCHEDULED", result
        assert result["reminder_result"]["mis_status"] == "applied"
        assert emulator.write_count == emulator.patch_count == 1
        assert emulator.records["demo"][1].status == VisitStatus.CANCELLED


@pytest.mark.parametrize(
    "answer", ["Не знаю.", "Ну да, наверное."]
)
@pytest.mark.parametrize("phase", ["move_offered", "move_confirm"])
async def test_uncertain_move_answer_clarifies_without_ending_or_mutating(
    reminder_engine, phase, answer
):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        await turn(client, 3, "Перенесите на следующую неделю")
        if phase == "move_confirm":
            await turn(client, 4, "Вторник")

        result = await turn(client, 5 if phase == "move_confirm" else 4, answer)

        assert result["phase"] == phase
        assert result["code"] == "REMINDER_CLARIFICATION"
        assert result["reminder_result"] is None
        assert emulator.write_count == emulator.patch_count == 0
        assert not actions.calls


async def test_opt_out_after_verification_is_separate_from_visit_cancellation(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        result = await turn(client, 3, "Не звоните мне больше")
        assert result["reminder_result"]["outcome"] == "OPT_OUT"
        assert actions.calls == [("opt_out", 1)]
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.write_count == emulator.patch_count == 0


async def test_voicemail_has_ordered_idempotent_receipt_and_no_details(reminder_engine):
    async with outgoing(reminder_engine) as (_, service, emulator, actions, context):
        await opened(service, context, verify=False)
        event = OutboundEvent(attempt_id="attempt-1", turn_id=2, kind="voicemail")
        with pytest.raises(DialogueError, match="OUTBOUND_ATTEMPT_CONFLICT"):
            await service.outbound_event(
                "scenario", event.model_copy(update={"attempt_id": "other"})
            )
        result = await service.outbound_event("scenario", event)
        assert result.reminder_result.outcome == "VOICEMAIL"
        assert "Ольга" not in " ".join(result.utterances)
        assert result == await service.outbound_event("scenario", event)
        assert not actions.calls and emulator.write_count == emulator.patch_count == 0


async def test_repeated_unknown_input_exits_instead_of_looping(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context, verify=False)
        for number in (2, 3, 4):
            result = await turn(client, number, "абракадабра")
        assert result["phase"] == "handoff" and result["transfer"]["route"] == "R-05"
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert not actions.calls and emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("text", ["Давит в груди", "Соедините с оператором"])
async def test_global_safety_preempts_outgoing_dialogue(reminder_engine, text):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        result = await turn(client, 3, text, final=False)
        assert result["phase"] == "handoff"
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert not actions.calls and emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("interruption", ["speech", "hangup", "lease"])
async def test_waiting_mark_loses_permission_on_new_input_or_hangup(reminder_engine, interruption):
    async with outgoing(reminder_engine) as (_, service, _, actions, context):
        await opened(service, context)
        actions.gate = asyncio.Event()
        task = asyncio.create_task(service.handle("scenario", Turn(turn_id=3, text="Да")))
        await asyncio.wait_for(actions.entered.wait(), 2)
        if interruption == "speech":
            await service.input_started("scenario", 4)
        elif interruption == "hangup":
            await service.end("scenario")
        else:
            actions.valid = False
        actions.gate.set()
        await asyncio.wait_for(task, 2)
        assert not actions.calls


async def test_identity_and_other_visits_cannot_reuse_outgoing_permission(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, emulator, actions, context):
        await opened(service, context)
        result = await turn(client, 3, "Покажите все мои записи")
        assert result["code"] == "REMINDER_SCOPE"
        result = await turn(client, 4, "Иванов Иван Иванович, 01.01.1990")
        assert result["reminder_result"]["outcome"] == "ESCALATED"
        assert not actions.calls and emulator.write_count == emulator.patch_count == 0


async def test_faq_resumes_the_actual_question_without_mis_requests(reminder_engine):
    async with outgoing(reminder_engine) as (client, service, _, actions, context):
        await opened(service, context)
        seen = []

        async def capture(request):
            seen.append(request.url.path)

        service.booking.mis._client.event_hooks["request"].append(capture)
        result = await turn(client, 3, "Какой у вас адрес?")
        assert result["code"] == "INFO_GIVEN" and result["question"]["key"] == "reminder_attendance"
        assert "Вы придёте" in " ".join(result["utterances"])
        assert seen == [] and not actions.calls


async def test_outbound_is_disabled_without_an_attempt_adapter():
    async with pipeline(SETUP) as (_, service, emulator, *_):
        context = OutboundContext(
            attempt_id="x",
            fence=1,
            client_id=1,
            visit=summary(emulator.records["demo"][1]),
            addressee="Ольга",
        )
        with pytest.raises(DialogueError, match="OUTBOUND_NOT_CONFIGURED"):
            await service.open_outbound("scenario", context)
