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


@pytest.mark.parametrize(
    "phrase, intent",
    [
        ("Запишите к терапевту", "I-01"),
        ("нужен талон к лору", "I-01"),
        ("хочу к гинекологу на следующей неделе", "I-01"),
        ("Не могу прийти в четверг, давайте на другой день", "I-02"),
        ("перезапишите меня", "I-02"),
        ("Отмените запись", "I-03"),
        ("я не приду", "I-03"),
        ("уже не нужно", "I-03"),
        ("На когда я записан?", "I-04"),
        ("во сколько мне к врачу?", "I-04"),
        ("какой кабинет?", "I-04"),
        ("Вызвать врача на дом", "I-05"),
        ("ребёнок заболел, нужен педиатр домой", "I-05"),
        ("Когда принимает Кузнецова?", "I-06"),
        ("до скольки вы работаете?", "I-06"),
        ("работаете в субботу?", "I-06"),
        ("Готовы ли мои анализы?", "I-07"),
        ("пришли результаты крови?", "I-07"),
        ("Сколько стоит УЗИ?", "I-08"),
        ("можно платно к кардиологу?", "I-08"),
        ("Нет талонов — запишите в очередь", "I-09"),
        ("позвоните, когда появится время", "I-09"),
        ("Как прикрепиться?", "I-10"),
        ("что взять на приём?", "I-10"),
        ("нужна справка в бассейн", "I-10"),
        ("Боль в груди", "I-11"),
        ("не может дышать", "I-11"),
        ("потерял сознание", "I-11"),
        ("Хочу пожаловаться", "I-12"),
        ("безобразие", "I-12"),
        ("врач нагрубил", "I-12"),
        ("Что выпить от давления?", "I-13"),
        ("нормальный ли у меня холестерин?", "I-13"),
        ("Запрос от организации", "I-14"),
        ("выписка из медкарты", "I-14"),
        ("сложная ситуация", "I-14"),
    ],
)
def test_regulation_intent_table_examples(phrase, intent):
    assert intent_of(phrase) == intent


@pytest.mark.parametrize(
    "phrase, intent",
    [
        # отрицание при глаголе меняет смысл, отрицание рядом — нет
        ("Не переносите, просто отмените", "I-03"),
        ("Нет, не надо переносить, отменяйте", "I-03"),
        ("Не отменяйте, лучше перенесите на пятницу", "I-02"),
        ("Не могу прийти, давайте перенесём", "I-02"),
        ("Не надо переносить", None),
        ("Не отменяйте", None),
    ],
)
def test_negated_action(phrase, intent):
    assert intent_of(phrase) == intent


def test_place_alone_counts_only_when_asked():
    assert intent_of("Которая к лору", by_place=True) == "I-01"
    assert intent_of("Которая к лору", by_place=False) is None
    assert intent_of("Нет, не надо, запишите меня к окулисту", by_place=False) == "I-01"
