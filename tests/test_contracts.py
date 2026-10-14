"""Проверяем границы сообщений, важные для безопасной стыковки с NLU и голосом."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.contracts import (
    ActionConfirmation,
    DialogOption,
    NluContext,
    NluRequest,
    NluResult,
    NluSlots,
    SafetyDecision,
    VoiceInput,
)


def test_nlu_accepts_actual_local_parser_format():
    result = NluResult(
        call_id="test-call",
        turn_id=1,
        slots={
            "specialty": "оториноларинголог",
            "exclude_weekdays": [5],
            "time_from": "12:00",
            "time_to": "16:00",
        },
        evidence={
            "specialty": "лору",
            "exclude_weekdays": "не в пятницу",
            "time_from": "после обеда",
            "time_to": "после обеда",
        },
        source="rules",
    )
    assert result.slots.exclude_weekdays == (5,)
    assert result.slots.time_from.hour == 12
    assert NluResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("evidence", [{}, {"doctor": "Кузнецова"}, {"specialty": " "}])
def test_extracted_slot_requires_its_own_evidence(evidence):
    with pytest.raises(ValidationError):
        NluResult(
            call_id="test",
            turn_id=1,
            slots={"specialty": "терапевт"},
            evidence=evidence,
            source="rules",
        )


def test_birth_date_alternatives_match_ilya_parser():
    result = NluResult(
        call_id="test",
        turn_id=1,
        slots={"birth_date": "2020-04-07"},
        evidence={"birth_date": "седьмое апреля двадцатого"},
        alternatives={"birth_date": ["1920-04-07"]},
        source="rules",
    )
    assert result.alternatives["birth_date"] == (date(1920, 4, 7),)


def test_birth_date_alternatives_require_base_date():
    with pytest.raises(ValidationError):
        NluResult(
            call_id="test",
            turn_id=1,
            source="rules",
            alternatives={"birth_date": ["1920-04-07"]},
        )


def test_context_rejects_identity_fields():
    with pytest.raises(ValidationError):
        NluContext(slots={"last_name": "Смирнова", "birth_date": "1978-03-12"})
    with pytest.raises(ValidationError):
        NluContext(slots=NluSlots(last_name="Смирнова"))


def test_personal_values_are_not_in_repr():
    slots = NluSlots(
        last_name="Смирнова", birth_date="1978-03-12", policy_number="1234567890123456"
    )
    assert all(value not in repr(slots) for value in ("Смирнова", "1978", "1234567890123456"))


@pytest.mark.parametrize(
    "slot", [{"unknown": "value"}, {"exclude_weekdays": [0]}, {"choice_index": 0}]
)
def test_unknown_or_out_of_range_slots_are_rejected(slot):
    with pytest.raises(ValidationError):
        NluSlots.model_validate(slot)


@pytest.mark.parametrize("turn_id", [0, -1, True, "1"])
def test_turn_id_is_a_positive_integer(turn_id):
    with pytest.raises(ValidationError):
        SafetyDecision(call_id="test", turn_id=turn_id)


def test_safety_is_pending_by_default_and_rejects_unknown_flags():
    assert not SafetyDecision(call_id="test", turn_id=1).final
    with pytest.raises(ValidationError):
        SafetyDecision(call_id="test", turn_id=1, flags=["seems_safe"])


def test_no_more_than_three_options():
    with pytest.raises(ValidationError):
        NluRequest(
            call_id="test",
            turn_id=1,
            text="второй",
            today=date(2026, 10, 14),
            options=[DialogOption(id=f"slot-{i}") for i in range(4)],
        )


@pytest.mark.parametrize(
    "started, ended",
    [
        ("2026-10-14T10:00:00", "2026-10-14T10:01:00+04:00"),
        ("2026-10-14T10:01:00+04:00", "2026-10-14T10:00:00+04:00"),
    ],
)
def test_voice_timestamps_have_timezone_and_correct_order(started, ended):
    with pytest.raises(ValidationError):
        VoiceInput(
            call_id="test",
            turn_id=1,
            text="да",
            is_final=True,
            started_at=started,
            ended_at=ended,
        )


def test_confirmation_requires_call_action_revision_and_final_turn():
    with pytest.raises(ValidationError):
        ActionConfirmation(action_id="action-1", revision=1, final_turn_id=2)
    with pytest.raises(ValidationError):
        ActionConfirmation(call_id="test", action_id="action-1", revision=0, final_turn_id=2)
