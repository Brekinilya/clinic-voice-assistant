"""Ошибки из аудита: смысл даты и выбор по настоящим ФИО из справочника."""

from datetime import date, time

import pytest

from app.contracts import DialogOption, NluRequest
from app.nlu.parsers import Option, parse_choice, parse_dates, parse_times
from app.nlu.service import RulesNlu

TODAY = date(2026, 10, 15)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("на 24.10.2027", date(2027, 10, 24)),
        ("на 24.10.27", date(2027, 10, 24)),
        ("на 24.10.2025", date(2025, 10, 24)),
        ("на двадцать четвёртое октября две тысячи двадцать седьмого года", date(2027, 10, 24)),
        ("на 29.02.2028", date(2028, 2, 29)),
        ("на 02.01", date(2027, 1, 2)),
    ],
)
def test_explicit_year_is_preserved(text, expected):
    found = parse_dates(text, TODAY)
    assert found["date_from"][0] == found["date_to"][0] == expected.isoformat()


@pytest.mark.parametrize("text", ["только не завтра", "не на завтра", "кроме завтра"])
def test_negated_relative_day_is_an_exclusion(text):
    found = parse_dates(text, TODAY)
    assert "date_from" not in found and "date_to" not in found
    assert found["exclude_dates"][0] == ["2026-10-16"]


def test_nonadjacent_weekdays_keep_the_gap_closed():
    found = parse_dates("в понедельник или в среду", TODAY)
    assert found["date_from"][0] == "2026-10-19"
    assert found["date_to"][0] == "2026-10-21"
    assert 2 in found["exclude_weekdays"][0]


def test_doctor_surname_selects_full_name_option():
    options = [
        Option(doctor="Кузнецова Анна Сергеевна"),
        Option(doctor="Петров Иван Петрович"),
    ]
    assert parse_choice("к Кузнецовой", options, TODAY)[0] == 1


def test_shared_surname_requires_another_constraint():
    options = [
        Option(at=time(10), doctor="Кузнецова Анна Сергеевна"),
        Option(at=time(11), doctor="Кузнецова Ирина Петровна"),
    ]
    assert parse_choice("к Кузнецовой", options, TODAY) is None
    assert parse_choice("к Кузнецовой в одиннадцать", options, TODAY)[0] == 2
    assert parse_choice("Кузнецова Ирина Петровна", options, TODAY)[0] == 2
    assert parse_choice("Кузнецова Мария Ивановна", options, TODAY) is None


@pytest.mark.parametrize(
    "text, positive, negative",
    [
        ("не завтра, а послезавтра", "2026-10-17", ["2026-10-16"]),
        ("кроме 24.10.2027", None, ["2027-10-24"]),
        ("не на двадцать четвёртое октября", None, ["2026-10-24"]),
    ],
)
def test_positive_and_negative_dates_are_kept_separate(text, positive, negative):
    found = parse_dates(text, TODAY)
    assert found.get("date_from", (None, ""))[0] == positive
    assert found["exclude_dates"][0] == negative


def test_weekday_range_keeps_its_middle_day():
    found = parse_dates("с понедельника по среду", TODAY)
    assert "exclude_weekdays" not in found


def test_specialty_choice_uses_the_schedule_alias():
    options = [Option(specialty="ЛОР (отоларинголог)"), Option(specialty="терапевт")]
    assert parse_choice("к лору", options, TODAY)[0] == 1
    assert parse_choice("к неврологу", options, TODAY) is None


@pytest.mark.parametrize("text", ["не завтра", "на 16.10.2027"])
def test_choice_does_not_select_a_forbidden_or_different_year_date(text):
    options = [Option(day=date(2026, 10, 16)), Option(day=date(2026, 10, 17))]
    selected = parse_choice(text, options, TODAY)
    assert selected is None or (text == "не завтра" and selected[0] == 2)


def test_option_number_does_not_override_a_conflicting_day():
    options = [Option(day=date(2026, 10, 16)), Option(day=date(2026, 10, 17))]
    assert parse_choice("первый, но в субботу", options, TODAY) is None
    assert parse_choice("на первый вариант", options, TODAY)[0] == 1


def test_numeric_date_is_not_also_a_clock_time():
    assert parse_times("на 16.10.2026", bare=True) == {}
    found = parse_times("на 16.10.2026 в десять тридцать")
    assert found["time_from"][0] == found["time_to"][0] == "10:30"


def test_dot_separated_time_with_an_impossible_month_still_works():
    assert parse_times("в 10.30")["time_from"][0] == "10:30"


def test_choice_matches_the_full_numeric_date_including_its_year():
    options = [
        Option(day=date(2026, 10, 16), at=time(10)),
        Option(day=date(2027, 10, 16), at=time(10)),
    ]
    assert parse_choice("на 16.10.2027", options, TODAY)[0] == 2


def test_excluded_dates_survive_the_typed_nlu_contract():
    result = RulesNlu().parse(NluRequest(call_id="dates", turn_id=1, text="не завтра", today=TODAY))
    assert result.slots.exclude_dates == (date(2026, 10, 16),)
    assert result.evidence["exclude_dates"]
    assert type(result).model_validate_json(result.model_dump_json()) == result


def test_full_name_options_work_through_rules_nlu():
    result = RulesNlu().parse(
        NluRequest(
            call_id="doctors",
            turn_id=1,
            text="к Кузнецовой",
            today=TODAY,
            expect="choice",
            options=(
                DialogOption(id="one", doctor="Кузнецова Анна Сергеевна"),
                DialogOption(id="two", doctor="Петров Иван Петрович"),
            ),
        )
    )
    assert result.intents == ("choice",)
    assert result.slots.choice_index == 1
