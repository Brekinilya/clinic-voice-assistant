"""Метаданные вопроса совпадают с NLU и не превращают контекст в полномочия."""

import pytest

from app.dialogue.expectation import active_question, expected_answer
from app.dialogue.models import DialogueState, Phase, Reply, reply


@pytest.mark.parametrize(
    ("changes", "key", "expected"),
    [
        ({"phase": Phase.READY}, "goal", "free"),
        ({"phase": Phase.IDENTIFYING}, "patient_identity", "full_name"),
        ({"phase": Phase.IDENTIFYING, "identity_draft": "sealed"}, "birth_date", "birth_date"),
        ({"phase": Phase.BOOKED}, "more_help", "yes_no"),
        ({"phase": Phase.CANCEL_OFFER}, "cancel_or_move", "free"),
        ({"phase": Phase.VISITS_SHOWN, "visit_has_more": True}, "visit_next_page", "yes_no"),
        ({"phase": Phase.VISITS_SHOWN}, "more_help", "yes_no"),
    ],
)
def test_semantic_question_is_shared_by_nlu_and_reply(changes, key, expected):
    state = DialogueState(call_id="question", **changes)
    result = reply(state, 1, "Тестовый вопрос.")
    assert result.question.key == key
    assert result.question == active_question(state)
    assert expected_answer(state) == result.expect == result.question.expect == expected
    assert state.authorization is None


def test_clarifying_information_and_medical_question_override_business_yes():
    state = DialogueState(call_id="question", phase=Phase.HELD, medical_boundary_pending=True)
    assert active_question(state).key == "medical_booking"
    assert active_question(state).proposal_id is None
    state = state.model_copy(update={"information_clarifying": True})
    assert active_question(state).key == "information_details"
    assert expected_answer(state) == "free"


def test_old_receipt_without_metadata_is_readable_and_never_an_authorization():
    old = reply(DialogueState(call_id="question"), 1, "Чем помочь?").model_dump(mode="json")
    old.pop("question")
    restored = Reply.model_validate(old)
    assert restored.question is None
    assert active_question(DialogueState(call_id="question", phase=Phase.ENDED)) is None
