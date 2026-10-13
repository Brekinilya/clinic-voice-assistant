import pytest

from app.nlu.catalog import load_catalog
from app.nlu.intents import classify
from app.policies.safety import check

CATALOG = load_catalog()
EXAMPLES = [(code, ex) for code, intent in CATALOG.intents.items() for ex in intent.examples]
ANSWERS = [ex for intent in CATALOG.service_intents.values() for ex in intent.examples]


def intent_of(text: str, by_place: bool = True) -> str | None:
    found = classify(text, check(text).flags, by_place=by_place)
    return found[0] if found else None


@pytest.mark.parametrize("code, example", EXAMPLES, ids=[ex for _, ex in EXAMPLES])
def test_catalog_examples(code, example):
    assert intent_of(example) == code


@pytest.mark.parametrize("answer", ANSWERS)
def test_short_answers_have_no_topic(answer):
    assert intent_of(answer) is None


@pytest.mark.parametrize(
    "phrase, intent",
    [
        # слова двух интентов: побеждает более конкретный
        ("Я записана на четверг, хочу перенести", "I-02"),
        ("Нет талонов, запишите в очередь", "I-09"),
        ("Не могу прийти, давайте на другой день", "I-02"),
        ("Сколько стоит справка для бассейна?", "I-08"),
        ("Запишите к терапевту, у меня сильно давит в груди", "I-11"),
        ("Безобразие, хочу записаться и не могу", "I-12"),
        ("В какие дни принимает лор?", "I-06"),
        ("Какие таблетки принимать от аллергии?", "I-13"),
    ],
)
def test_precedence(phrase, intent):
    assert intent_of(phrase) == intent


def test_place_alone_counts_only_when_asked():
    assert intent_of("Которая к лору", by_place=True) == "I-01"
    assert intent_of("Которая к лору", by_place=False) is None
    assert intent_of("Нет, не надо, запишите меня к окулисту", by_place=False) == "I-01"
