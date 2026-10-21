import pytest

from app.dialogue.cross_scenario import CrossScenario
from app.dialogue.cross_scenario import classify as classify_cross_scenario
from app.dialogue.introduction import classify_introduction
from app.dialogue.triage import needs_urgent_screening
from app.dialogue.visit_privacy import requested_owner


@pytest.mark.parametrize(
    "phrase",
    [
        "Что вы умеете?",
        "Вы кто?",
        "Кто вы?",
        "Как вас зовут?",
    ],
)
def test_capability_questions_are_not_unknown(phrase):
    assert classify_introduction(phrase) == "capabilities"


@pytest.mark.parametrize(
    "phrase",
    [
        "Как записаться к врачу?",
        "Я хочу узнать, как записаться к врачу",
        "Подскажите, как можно записаться",
    ],
)
def test_booking_instructions_are_not_booking_commands(phrase):
    assert classify_introduction(phrase) == "booking_guidance"


def test_general_information_request_prompts_for_topic():
    assert classify_introduction("Я хочу справочную информацию") == "general_help"


@pytest.mark.parametrize(
    "phrase",
    [
        "Я просто спросить хочу",
        "Я только хочу узнать",
        "Я просто хотела спросить",
    ],
)
def test_user_can_back_out_of_booking_to_ask_a_general_question(phrase):
    assert classify_introduction(phrase) == "booking_abandoned"


def test_unsupported_service_is_rejected_before_identity_collection():
    assert classify_introduction("У вас можно на массаж записаться") == "unsupported_service"


@pytest.mark.parametrize(
    "phrase",
    [
        "Запишите меня к хирургу",
        "Я хочу записаться к врачу",
        "Позовите оператора",
        "У меня болит живот",
    ],
)
def test_specific_requests_are_not_rewritten_as_introduction(phrase):
    assert classify_introduction(phrase) is None


@pytest.mark.parametrize(
    "phrase",
    [
        "Мои записи",
        "Посмотреть мои записи",
        "Хочу свою запись посмотреть",
        "Хочу найти свою запись",
        "Хочу проверить свои приёмы",
    ],
)
def test_natural_personal_visit_queries_switch_to_visit_route(phrase):
    assert classify_cross_scenario(phrase) == CrossScenario.SWITCH_VISITS
    assert requested_owner(phrase) == "self"


@pytest.mark.parametrize("phrase", ["Побаливает живот", "Я умираю", "Мне очень плохо"])
def test_unclear_distress_gets_a_triage_route_instead_of_unknown(phrase):
    assert needs_urgent_screening(phrase)


@pytest.mark.parametrize("phrase", ["Запишите к хирургу", "Как получить результаты анализов?"])
def test_non_symptom_request_is_not_sent_to_urgent_screening(phrase):
    assert not needs_urgent_screening(phrase)
