import time

import pytest

from app.nlu.dictionaries import FLAGS_PATH, read_yaml
from app.policies.safety import FLAGS, RULES, SafetyVerdict, check, with_flags
from app.voice.phrases import load_phrasebook


@pytest.mark.parametrize(
    "phrase, flags, rule",
    [
        # приложение Д: контрольные диалоги 1, 2, 3, 5, 14, 15, 16
        ("ой, у меня вообще-то сильно давит в груди", {"red_a"}, "R-01"),
        ("Что мне выпить от головной боли?", {"medical"}, None),
        ("Какой у меня холестерин в последнем анализе?", {"medical"}, None),
        ("Соедините с человеком", {"operator"}, "R-02"),
        ("Вы робот?", {"robot"}, None),
        ("Я против записи разговора", {"no_recording"}, None),
        ("Хочу пожаловаться на регистратуру", {"complaint"}, "R-03"),
        ("Запишите к терапевту", set(), None),
    ],
)
def test_control_dialogs(phrase, flags, rule):
    verdict = check(phrase)
    assert verdict.flags == flags
    assert verdict.rule == rule


def test_red_flag_beats_everything():
    verdict = check("соедините с оператором, у мужа болит в груди")
    assert verdict.flags == {"red_a", "operator"}
    assert verdict.rule == "R-01"
    assert verdict.emergency


def test_red_b_before_operator_and_complaint():
    assert check("безобразие, температура не сбивается, соедините с человеком").rule == "R-11"


def test_red_b_outside_operator_hours_is_emergency():
    assert check("температура сорок", operators_on_duty=True).rule == "R-11"
    assert check("температура сорок", operators_on_duty=False).rule == "R-01"


def test_tooth_pain_goes_to_dentistry():
    verdict = check("сильно болит зуб")
    assert verdict.flags == {"dental_acute", "red_b"}
    assert verdict.rule == "R-09"
    assert check("сильно болит зуб", operators_on_duty=False).rule == "R-09"


def test_tooth_pain_with_other_red_b_stays_urgent():
    assert check("сильно болит зуб и температура сорок").rule == "R-11"


def test_hits_carry_evidence():
    verdict = check("мужу плохо, давит в груди")
    assert [(h.key, h.phrase) for h in verdict.hits] == [("red_a.heart", "давит в груди")]


def test_general_parse_can_only_add_flags():
    quiet = check("мне как-то нехорошо, всё плывёт перед глазами", final=False)
    assert quiet.rule is None and not quiet.final
    raised = with_flags(quiet, {"red_b"})
    assert raised.rule == "R-11" and raised.final

    alarmed = check("болит в груди")
    assert with_flags(alarmed, set()).flags == {"red_a"}


def test_unknown_flag_rejected():
    with pytest.raises(ValueError):
        with_flags(SafetyVerdict(), {"red_c"})


def test_flags_match_dictionaries():
    assert set(FLAGS) == {"red_a", "red_b"} | set(read_yaml(FLAGS_PATH)["flags"])


def test_rules_have_phrases():
    routes = {t.route for t in load_phrasebook().templates.values()}
    assert {rule for _, rule in RULES} <= routes


def test_fast_enough():
    check("разогрев словарей")
    started = time.perf_counter()
    for _ in range(20):
        check(
            "здравствуйте, мне бы записаться к терапевту на завтра с утра, если можно, пожалуйста"
        )
    assert (time.perf_counter() - started) / 20 < 0.05
