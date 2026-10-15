from datetime import date, datetime

import pytest

from app.nlu.catalog import load_catalog
from app.nlu.lexicon import tokenize
from app.nlu.parsers import (
    Option,
    parse_answer,
    parse_birth_date,
    parse_choice,
    parse_dates,
    parse_digits,
    parse_doctor,
    parse_expected,
    parse_full_name,
    parse_times,
    spoken_numbers,
)

TODAY = date(2026, 9, 30)  # среда, как в примерах регламента
NAME_SLOTS = {"last_name", "first_name", "middle_name", "birth_date"}
GENERAL_ONLY = {"visit_ref", "on_behalf", "service"}  # такие параметры извлекает общий разбор
DOCTORS = [
    "Кузнецова",
    "Кузнецов",
    "Смирнова",
    "Орлова",
    "Васильева",
    "Белова",
    "Громов",
    "Попова",
    "Морозова",
    "Егоров",
    "Медведева",
    "Волков",
    "Никитина",
    "Фёдорова",
    "Захаров",
    "Лебедев",
    "Киселёв",
    "Соколова",
]

CATALOG = load_catalog()


def values(found):
    return {name: value for name, (value, _) in found.items()}


# Примеры параметров из config/intents.yaml


@pytest.mark.parametrize("case", CATALOG.slot_examples, ids=lambda c: c["say"])
def test_catalog_slot_examples(case):
    expected = case["slots"]
    if set(expected) & GENERAL_ONLY:
        pytest.skip("не для локального разбора")
    if "policy_number" in expected:
        assert parse_digits(case["say"]) == expected["policy_number"]
        return
    expect = "full_name" if set(expected) & NAME_SLOTS else "free"
    parsed = parse_expected(
        case["say"], expect, date.fromisoformat(CATALOG.reference_date), doctors=DOCTORS
    )
    assert parsed.slots == expected
    assert set(parsed.evidence) == set(parsed.slots)


@pytest.mark.parametrize(
    "group, case",
    [(g, c) for g in CATALOG.choice_examples for c in g["cases"]],
    ids=lambda x: x["say"] if "say" in x else "",
)
def test_catalog_choice_examples(group, case):
    options = [
        Option(day=dt.date(), at=dt.time()) for dt in map(datetime.fromisoformat, group["options"])
    ]
    chosen = parse_choice(case["say"], options, TODAY)
    assert chosen is not None and chosen[0] == case["choice_index"]


@pytest.mark.parametrize("intent", ["yes", "no", "repeat", "maybe", "opt_out"])
def test_catalog_short_answers(intent):
    for example in CATALOG.service_intents[intent].examples:
        assert parse_answer(example) == intent, example


def test_unknown_stays_unknown():
    for example in CATALOG.service_intents["unknown"].examples:
        assert parse_answer(example) is None, example


# Числа


def test_spoken_numbers():
    found = spoken_numbers(tokenize("двенадцать ноль три семьдесят восемь"))
    assert [n.digits for n in found] == ["12", "03", "78"]
    assert [n.value for n in spoken_numbers(tokenize("тысяча девятьсот семьдесят восьмого"))] == [
        1978
    ]
    assert [n.value for n in spoken_numbers(tokenize("две тысячи девятнадцатого"))] == [2019]
    assert [n.value for n in spoken_numbers(tokenize("десять тридцать"))] == [10, 30]
    assert [n.value for n in spoken_numbers(tokenize("в двадцать один пятнадцать"))] == [21, 15]


def test_policy_number():
    assert parse_digits("1234 5678 9012 3456") == "1234567890123456"
    assert parse_digits("один два три") is None


# Дата рождения: двузначный год восстанавливается так, чтобы пациенту было не больше 110 лет


@pytest.mark.parametrize(
    "text, value, alternatives",
    [
        ("пятое мая двадцатого", "2020-05-05", ["1920-05-05"]),
        ("первое января девятнадцатого", "2019-01-01", ["1919-01-01"]),
        ("десятое мая пятнадцатого", "2015-05-10", []),
        ("первое декабря двадцать шестого", "1926-12-01", []),
        ("12 марта 1978 года", "1978-03-12", []),
        ("двенадцатого третьего семьдесят восьмого", "1978-03-12", []),
        ("родилась девятого февраля восемьдесят пятого", "1985-02-09", []),
    ],
)
def test_birth_date(text, value, alternatives):
    born = parse_birth_date(text, TODAY)
    assert born.value.isoformat() == value
    assert [d.isoformat() for d in born.alternatives] == alternatives


@pytest.mark.parametrize(
    "text", ["тридцать первое февраля восьмидесятого", "двенадцатое марта", "не помню"]
)
def test_birth_date_rejected(text):
    assert parse_birth_date(text, TODAY) is None


# Желаемые дни


@pytest.mark.parametrize(
    "text, expected",
    [
        ("на следующей неделе в пятницу", {"date_from": "2026-10-09", "date_to": "2026-10-09"}),
        ("в следующую пятницу", {"date_from": "2026-10-09", "date_to": "2026-10-09"}),
        ("в эту пятницу", {"date_from": "2026-10-02", "date_to": "2026-10-02"}),
        ("через две недели", {"date_from": "2026-10-14", "date_to": "2026-10-14"}),
        ("через три дня", {"date_from": "2026-10-03", "date_to": "2026-10-03"}),
        ("на пятое", {"date_from": "2026-10-05", "date_to": "2026-10-05"}),
        ("на двадцать девятое число", {"date_from": "2026-10-29", "date_to": "2026-10-29"}),
        ("на 05.10", {"date_from": "2026-10-05", "date_to": "2026-10-05"}),
        ("в понедельник или во вторник", {"date_from": "2026-10-05", "date_to": "2026-10-06"}),
        ("только не в пятницу", {"exclude_weekdays": [5]}),
        ("после двадцатого", {"date_from": "2026-10-21"}),
        ("до пятого", {"date_to": "2026-10-05"}),
        ("с пятого по десятое октября", {"date_from": "2026-10-05", "date_to": "2026-10-10"}),
        ("в десять тридцать", {}),
    ],
)
def test_dates(text, expected):
    assert values(parse_dates(text, TODAY)) == expected


# Желаемое время


@pytest.mark.parametrize(
    "text, expected",
    [
        ("с девяти до двенадцати", {"time_from": "09:00", "time_to": "12:00"}),
        ("не раньше трёх", {"time_from": "15:00"}),
        ("не позже одиннадцати", {"time_to": "11:00"}),
        ("в половине десятого", {"time_from": "09:30", "time_to": "09:30"}),
        ("полдесятого", {}),
        ("к полдесятого", {"time_from": "09:30", "time_to": "09:30"}),
        ("в два часа дня", {"time_from": "14:00", "time_to": "14:00"}),
        ("в час", {"time_from": "13:00", "time_to": "13:00"}),
        ("к девяти", {"time_from": "09:00", "time_to": "09:00"}),
        ("в 15:30", {"time_from": "15:30", "time_to": "15:30"}),
        ("до обеда", {"time_to": "12:00"}),
        ("утром в десять", {"time_from": "10:00", "time_to": "10:00"}),
        ("с утра пораньше", {"time_from": "07:30", "time_to": "12:00", "time_pref": "early"}),
        ("на пятое октября", {}),
    ],
)
def test_times(text, expected):
    assert values(parse_times(text)) == expected


def test_bare_time_only_when_asked():
    assert values(parse_times("двенадцать ноль-ноль")) == {}
    assert values(parse_times("двенадцать ноль-ноль", bare=True)) == {
        "time_from": "12:00",
        "time_to": "12:00",
    }


# Короткие ответы


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Да-да, записывайте", "yes"),
        ("конечно", "yes"),
        ("нет проблем", "yes"),
        ("не против", "yes"),
        ("да нет, не надо", "no"),
        ("конечно нет", "no"),
        ("нет, на пятницу", "no"),
        ("нет, вы мне просто скажите, что можно выпить", "no"),
        ("да, пусть перезвонят", "yes"),
        ("можно, но нет", None),
        ("да, но лучше вечером", None),
        ("да, на пятницу", None),
        ("можно попозже?", None),
        ("ничего не понял", "repeat"),
        ("не звоните мне больше", "opt_out"),
        ("пока не знаю, может быть", "maybe"),
    ],
)
def test_answers(text, expected):
    assert parse_answer(text) == expected


# ФИО


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Ольга Викторовна Смирнова", ("Смирнова", "Ольга", "Викторовна")),
        ("смирнова ольга викторовна", ("Смирнова", "Ольга", "Викторовна")),
        ("Меня зовут Петров Игорь Николаевич", ("Петров", "Игорь", "Николаевич")),
        ("Смирнову Ольгу Викторовну", ("Смирнова", "Ольга", "Викторовна")),
        (
            "Кузьмина Нина Андреевна, двадцатое июля пятьдесят пятого",
            ("Кузьмина", "Нина", "Андреевна"),
        ),
        ("Белова Ольга Николаевна", ("Белова", "Ольга", "Николаевна")),
        ("Фёдорова Наталья Юрьевна", ("Фёдорова", "Наталья", "Юрьевна")),
        ("Цой Виктор Робертович", ("Цой", "Виктор", "Робертович")),
        ("Иванов Сергей", ("Иванов", "Сергей", None)),
    ],
)
def test_full_name(text, expected):
    found = values(parse_full_name(text))
    assert (found.get("last_name"), found.get("first_name"), found.get("middle_name")) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("к Кузнецову", "Кузнецов"),
        ("к Кузнецовой", "Кузнецова"),
        ("запишите к Медведевой", "Медведева"),
        ("у Громова есть время?", "Громов"),
        ("к федоровой", "Фёдорова"),
        ("к терапевту", None),
    ],
)
def test_doctor(text, expected):
    found = parse_doctor(text, DOCTORS)
    assert (found[0] if found else None) == expected


# Выбор из существующих записей

VISITS = [
    Option(
        day=date(2026, 10, 1),
        at=datetime(2026, 10, 1, 8, 45).time(),
        doctor="Кузнецова",
        specialty="терапевт",
    ),
    Option(
        day=date(2026, 10, 2),
        at=datetime(2026, 10, 2, 10, 40).time(),
        doctor="Белова",
        specialty="лор",
    ),
]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("к лору", 2),
        ("ту, что к терапевту", 1),
        ("к Беловой", 2),
        ("в четверг", 1),
        ("завтрашнюю, в восемь сорок пять", 1),
        ("на второе октября", 2),
        ("не знаю", None),
        ("к хирургу", None),
        ("которая пораньше", 1),
        ("первая не подходит", None),
    ],
)
def test_choice_between_visits(text, expected):
    chosen = parse_choice(text, VISITS, TODAY)
    assert (chosen[0] if chosen else None) == expected


# Разбор по ожидаемому ответу


def test_expected_free_phrase():
    parsed = parse_expected("Мне к лору, но лучше не в пятницу, после обеда", "free", TODAY)
    assert parsed.slots == {
        "specialty": "лор",
        "exclude_weekdays": [5],
        "time_from": "12:00",
        "time_to": "16:00",
    }
    assert parsed.evidence["specialty"] == "лору"


def test_expected_ident_with_alternative_century():
    parsed = parse_expected(
        "Смирнова Алиса Дмитриевна, седьмое апреля двадцатого", "full_name", TODAY
    )
    assert parsed.slots["birth_date"] == "2020-04-07"
    assert parsed.alternatives == {"birth_date": ["1920-04-07"]}


def test_expected_choice_refusal():
    options = [Option(day=date(2026, 10, 2), at=datetime(2026, 10, 2, 15, 15).time())]
    assert parse_expected("нет, другое время", "choice", TODAY, options=options).intent == "no"


def test_expected_empty_means_general_parse():
    assert parse_expected("а у вас парковка есть?", "yes_no", TODAY).empty
