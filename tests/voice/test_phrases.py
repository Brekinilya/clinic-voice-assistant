import copy

import pytest
import yaml

from app.nlu.catalog import load_catalog
from app.voice.phrases import (
    MAX_WORDS,
    PHRASES_PATH,
    PhrasesError,
    count_words,
    load_phrasebook,
    parse_phrasebook,
)


@pytest.fixture(scope="module")
def book():
    return load_phrasebook()


@pytest.fixture
def raw():
    with open(PHRASES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def texts(book):
    for t in book.templates.values():
        yield t.id, t.text
        if t.retry:
            yield t.id, t.retry


def test_templates_fit_word_limit(book):
    assert book.too_long() == []


def test_every_scenario_has_phrases(book):
    sources = {t.src for t in book.templates.values()}
    missing = {f"С-{n:02d}" for n in range(1, 16)} - sources
    assert not missing


def test_every_route_has_phrase(book):
    routes = {t.route for t in book.templates.values() if t.route}
    assert routes == {f"R-{n:02d}" for n in range(1, 18)}


def test_intent_routes_have_phrases(book):
    routes = {t.route for t in book.templates.values()}
    for intent in load_catalog().intents.values():
        assert intent.route is None or intent.route in routes, intent.code


def test_no_forbidden_wording(book):
    # раздел 3.7 и запрет голосового меню из раздела 3.2
    forbidden = [
        "я не знаю",
        "не к нам",
        "звоните позже",
        "талонов нет",
        "успокойтесь",
        "скорее всего",
        "нажмите",
    ]
    for tid, text in texts(book):
        lowered = text.lower()
        for phrase in forbidden:
            assert phrase not in lowered, tid


def test_medical_boundary_without_advice(book):
    # контрольный диалог 2: без препаратов, с предложением записи
    text = book.get("medical.boundary").text.lower()
    for word in ("таблет", "выпе", "приним", "препарат"):
        assert word not in text
    assert "записать" in text
    assert "сто три" in text


def test_outbound_phrases_without_personal_data(book):
    # контрольные диалоги 10 и 11: ни врача, ни даты, ни специальности
    assert book.get("remind.voicemail").placeholders() == []
    assert [name for name, _ in book.get("remind.wrong_person").placeholders()] == ["patient"]


def test_third_party_gets_no_details(book):
    # контрольный диалог 4
    third_party = book.get("my_visits.third_party")
    assert third_party.placeholders() == []
    assert "регистратора" in third_party.text.casefold()
    assert "sms" not in third_party.text.casefold()
    assert "my_visits.sms_sent" not in book.templates


def test_honest_robot_answer(book):
    # контрольный диалог 14
    robot = book.get("robot.answer")
    assert "виртуальный помощник" in robot.text
    assert robot.expect == "yes_no"


def test_operator_request_transfers_at_once(book):
    # контрольный диалог 5
    t = book.get("transfer.operator")
    assert t.route == "R-02"
    assert t.expect is None


def test_greeting_mentions_recording(book):
    for tid in ("greeting.inbound", "greeting.repeat_call", "remind.greeting"):
        assert "записывается" in book.get(tid).text, tid


def test_count_words():
    assert count_words("Немедленно звоните — сто три.") == 4
    assert count_words("акушер-гинеколог, SMS") == 2


def test_estimate_words(book):
    t = book.get("booking.confirm")
    # «Записываю: терапевт Кузнецова, пятница, второе октября, пятнадцать пятнадцать, кабинет двести четырнадцать. Верно?»
    assert book.estimate_words(t.text) == 13
    assert book.estimate_words("Есть {options}.", options=3) == 1 + 3 * 5 + 1


def test_limit_value():
    assert MAX_WORDS == 20


def test_unknown_placeholder_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["templates"]["recording.off"]["text"] = "Хорошо, {nobody}."
    with pytest.raises(PhrasesError):
        parse_phrasebook(broken)


def test_wrong_form_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["templates"]["dates.confirm"]["text"] = "На {date:datv}, верно?"
    with pytest.raises(PhrasesError):
        parse_phrasebook(broken)


def test_options_count_required(raw):
    broken = copy.deepcopy(raw)
    del broken["templates"]["booking.offer"]["options"]
    with pytest.raises(PhrasesError):
        parse_phrasebook(broken)


def test_too_many_options_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["templates"]["booking.offer"]["options"] = 4
    with pytest.raises(PhrasesError):
        parse_phrasebook(broken)


def test_final_phrase_does_not_wait(raw):
    broken = copy.deepcopy(raw)
    broken["templates"]["closing.goodbye"]["expect"] = "free"
    with pytest.raises(PhrasesError):
        parse_phrasebook(broken)
