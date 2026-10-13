from datetime import date, time

import pytest

from app.contracts import DialogOption, NluRequest, NluResult
from app.nlu.service import RulesNlu

TODAY = date(2026, 9, 30)
NLU = RulesNlu(["Кузнецова Анна Сергеевна", "Белова Ирина Олеговна", "Волков Артём Сергеевич"])


def ask(text: str, expect: str = "free", options=()) -> NluResult:
    request = NluRequest(
        call_id="call-1", turn_id=3, text=text, expect=expect, today=TODAY, options=options
    )
    return NLU.parse(request)


def test_identification_is_typed_and_hidden_from_repr():
    result = ask("Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого", "full_name")
    assert result.slots.last_name == "Смирнова"
    assert result.slots.birth_date == date(1978, 3, 12)
    assert result.source == "rules"
    assert set(result.evidence) == result.slots.model_fields_set
    assert "Смирнова" not in repr(result)
    assert NluResult.model_validate_json(result.model_dump_json()) == result


def test_booking_request_with_constraints():
    result = ask("Мне к лору, но лучше не в пятницу, после обеда")
    assert result.intents == ("I-01",)
    assert result.slots.specialty == "оториноларинголог"
    assert result.slots.exclude_weekdays == (5,)
    assert (result.slots.time_from, result.slots.time_to) == (time(12), time(16))


def test_doctor_from_directory():
    result = ask("Запишите к Кузнецовой на пятницу в десять тридцать")
    assert result.slots.doctor == "Кузнецова"
    assert result.slots.date_from == date(2026, 10, 2)
    assert result.slots.time_from == time(10, 30)


def test_choice_between_offered_options():
    options = (
        DialogOption(id="slot-1", day=date(2026, 10, 2), at=time(15, 15)),
        DialogOption(id="slot-2", day=date(2026, 10, 5), at=time(16)),
    )
    result = ask("Давайте понедельник", "choice", options)
    assert result.intents == ("choice",)
    assert result.slots.choice_index == 2


@pytest.mark.parametrize(
    "text, expect, intents",
    [
        ("Да, верно", "yes_no", ("yes",)),
        ("Нет, не надо, запишите меня к окулисту", "yes_no", ("no", "I-01")),
        ("Нет, спасибо", "free", ("no",)),
        ("Отменить, уже не нужно", "free", ("I-03",)),
        ("ой, у меня сильно давит в груди", "full_name", ("I-11",)),
        ("кх... м-м... это самое", "free", ()),
    ],
)
def test_intents(text, expect, intents):
    assert ask(text, expect).intents == intents


def test_flags_come_with_every_phrase():
    result = ask("ой, у меня вообще-то сильно давит в груди", "choice")
    assert result.flags == {"red_a"}


def test_policy_number_on_free_question():
    result = ask(
        "один два три четыре пять шесть семь восемь девять ноль один два три четыре пять шесть"
    )
    assert result.slots.policy_number == "1234567890123456"


def test_second_century_for_two_digit_year():
    result = ask("Смирнова Алиса Дмитриевна, седьмое апреля двадцатого", "full_name")
    assert result.slots.birth_date == date(2020, 4, 7)
    assert result.alternatives == {"birth_date": (date(1920, 4, 7),)}


def test_nothing_understood_is_empty():
    result = ask("а у вас парковка есть?", "yes_no")
    assert result.intents == ()
    assert result.slots.model_fields_set == set()
