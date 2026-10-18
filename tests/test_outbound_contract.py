"""Контракт задания не позволяет выдать намерение пациента за записанный результат."""

import pytest
from pydantic import ValidationError

from app.dialogue.models import DialogueState, Phase, Reply, reply
from app.dialogue.outbound import OutboundContext, OutboundEvent, ReminderResult
from app.mis.models import parse_datetime
from app.visits.models import VisitSummary


def context(**changes):
    return OutboundContext(
        **(
            {
                "attempt_id": "attempt-1",
                "fence": 1,
                "client_id": 1,
                "addressee": "Ольга Викторовна",
                "visit": VisitSummary(
                    visit_id=3,
                    doctor_id=2,
                    doctor_name="Тестовый Врач Иванович",
                    specialty="терапевт",
                    scheduled_at=parse_datetime("20.10.2026 10:00"),
                ),
            }
            | changes
        )
    )


def test_context_is_internal_scoped_and_does_not_leak_addressee_in_repr():
    value = context()
    assert value.client_id == 1 and value.visit.visit_id == 3
    assert "Ольга" not in repr(value)
    with pytest.raises(ValidationError):
        context(fence=0)
    with pytest.raises(ValidationError):
        context(endpoint="arbitrary-destination")
    with pytest.raises(ValidationError):
        context(addressee="Имя\nСекрет")


@pytest.mark.parametrize("outcome", ["CONFIRMED", "RESCHEDULED", "CANCELLED", "OPT_OUT"])
def test_mutation_result_needs_an_explicit_mis_status(outcome):
    with pytest.raises(ValidationError, match="статус фиксации"):
        ReminderResult(attempt_id="attempt-1", outcome=outcome)
    pending = ReminderResult(attempt_id="attempt-1", outcome=outcome, mis_status="pending")
    assert pending.mis_status != "applied"


@pytest.mark.parametrize(
    "phase,key",
    [
        (Phase.REMIND_VERIFY, "reminder_addressee"),
        (Phase.REMIND_DETAILS, "reminder_attendance"),
    ],
)
def test_reminder_questions_are_distinct_and_have_no_booking_proposal(phase, key):
    result = reply(DialogueState(call_id="outbound", phase=phase), 1, "Вопрос напоминания.")
    assert result.question.key == key and result.question.proposal_id is None
    assert result.reminder_result is None


def test_voicemail_event_is_bound_to_attempt_and_a_real_turn():
    with pytest.raises(ValidationError):
        OutboundEvent(attempt_id="attempt-1", turn_id=1, kind="voicemail")
    event = OutboundEvent(attempt_id="attempt-1", turn_id=2, kind="voicemail")
    assert event.model_dump() == {
        "attempt_id": "attempt-1",
        "turn_id": 2,
        "kind": "voicemail",
    }
    old = reply(DialogueState(call_id="old"), 1, "Чем помочь?").model_dump(mode="json")
    old.pop("reminder_result")
    assert Reply.model_validate(old).reminder_result is None
