"""Исправления «не X, а Y», отказ после дня и синонимы подтверждения, просмотра и оператора."""

from datetime import date

import pytest

from app.nlu.intents import classify
from app.nlu.parsers import parse_answer, parse_dates, parse_times
from app.policies.safety import check

TODAY = date(2026, 9, 30)  # среда


def values(found):
    return {name: value for name, (value, _) in found.items()}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Исключите четверг и пятницу", {"exclude_weekdays": [4, 5]}),
        ("Уберите понедельник и вторник", {"exclude_weekdays": [1, 2]}),
        ("Ни в среду, ни в пятницу", {"exclude_weekdays": [3, 5]}),
        (
            "В четверг не могу, давайте в пятницу",
            {"date_from": "2026-10-02", "date_to": "2026-10-02", "exclude_weekdays": [4]},
        ),
        (
            "Сегодня уже поздно, давайте завтра",
            {"date_from": "2026-10-01", "date_to": "2026-10-01", "exclude_dates": ["2026-09-30"]},
        ),
        (
            "Не надо двадцать первого, лучше двадцать второго",
            {"date_from": "2026-10-22", "date_to": "2026-10-22", "exclude_dates": ["2026-10-21"]},
        ),
        (
            "Лучше не двадцать первого, а двадцать второго",
            {"date_from": "2026-10-22", "date_to": "2026-10-22", "exclude_dates": ["2026-10-21"]},
        ),
        # Отрицание не распространяется дальше своей части фразы
        ("в пятницу не позже трёх", {"date_from": "2026-10-02", "date_to": "2026-10-02"}),
        (
            "не в понедельник и во вторник",
            {"date_from": "2026-10-06", "date_to": "2026-10-06", "exclude_weekdays": [1]},
        ),
        # Порядковое число без месяца само по себе не дата: «второго варианта»
        ("давайте второго варианта", {}),
    ],
)
def test_negated_days(text, expected):
    assert values(parse_dates(text, TODAY)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Нет, не в шесть, а в восемь вечера", {"time_from": "20:00", "time_to": "20:00"}),
        ("Мне бы после работы, часов в семь", {"time_from": "19:00", "time_to": "19:00"}),
        ("в семь вечером", {"time_from": "19:00", "time_to": "19:00"}),
        ("в семь утра", {"time_from": "07:00", "time_to": "07:00"}),
        ("в девять", {"time_from": "09:00", "time_to": "09:00"}),
        ("не в шесть", {}),
    ],
)
def test_corrected_times(text, expected):
    assert values(parse_times(text)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Да, подтверждаю", "yes"),
        ("Хорошо, договорились", "yes"),
        ("Подтверждаю", "yes"),
        ("да, но лучше в четверг", None),
    ],
)
def test_confirmation_synonyms(text, expected):
    assert parse_answer(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Покажите мою запись", "I-04"),
        ("Посмотреть мою запись", "I-04"),
        ("Напомните мою запись на среду", "I-04"),
        ("Покажите мои записи на этой неделе", "I-04"),
        ("Отмените мою запись", "I-03"),
        ("Посмотрите, есть ли запись к терапевту на пятницу", "I-01"),
    ],
)
def test_view_phrases(text, expected):
    found = classify(text, check(text).flags)
    assert found is not None and found[0] == expected


@pytest.mark.parametrize("text", ["Отменять ничего не будем", "Нет, не будем отменять"])
def test_refusing_to_cancel_is_not_cancel(text):
    found = classify(text, check(text).flags)
    assert found is None or found[0] != "I-03"


@pytest.mark.parametrize(
    "text, operator",
    [
        ("Позовите администратора", True),
        ("Переключите на администратора", True),
        ("Соедините с администрацией", False),
        ("Соедините с платным отделением", False),
    ],
)
def test_administrator_is_a_person(text, operator):
    assert ("operator" in check(text).flags) is operator
