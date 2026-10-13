import copy
import re

import pytest
import yaml

from app.nlu.catalog import CATALOG_PATH, SERVICE_INTENTS, CatalogError, load_catalog, parse_catalog

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HH_MM = re.compile(r"^\d{2}:\d{2}$")


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture
def raw():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_all_intents_from_regulation(catalog):
    assert list(catalog.intents) == [f"I-{n:02d}" for n in range(1, 15)]


def test_service_intents(catalog):
    assert set(catalog.service_intents) == set(SERVICE_INTENTS)


def test_inbound_scenarios_covered(catalog):
    covered = {s for intent in catalog.intents.values() for s in intent.scenarios}
    # С-10 — исходящее напоминание, входящего интента у него нет
    assert covered == {f"С-{n:02d}" for n in range(1, 16)} - {"С-10"}


def test_identification_as_in_regulation(catalog):
    # справки общего характера (режим работы, цены, график) — без идентификации, раздел 3.5
    no_ident = {code for code, intent in catalog.intents.items() if not intent.identify}
    assert {"I-06", "I-08", "I-10", "I-11", "I-12", "I-13", "I-14"} == no_ident


def test_operator_intents_have_routes(catalog):
    routes = {code: intent.route for code, intent in catalog.intents.items() if intent.route}
    assert routes == {"I-08": "R-10", "I-11": "R-01", "I-12": "R-03", "I-13": "R-04", "I-14": "R-08"}


def test_example_belongs_to_one_intent(catalog):
    owners = {}
    for code in catalog.all_intent_codes():
        intent = catalog.intents.get(code) or catalog.service_intents[code]
        for example in intent.examples:
            key = example.lower().strip(" ?!.")
            assert key not in owners, f"«{example}» есть и в {owners[key]}, и в {code}"
            owners[key] = code


def test_slot_example_values(catalog):
    for case in catalog.slot_examples:
        for name, value in case["slots"].items():
            slot = catalog.slots[name]
            if slot.type == "date":
                assert ISO_DATE.match(value), case["say"]
            elif slot.type == "time":
                assert isinstance(value, str) and HH_MM.match(value), case["say"]
            elif slot.type == "enum":
                assert value in slot.values, case["say"]
            elif slot.type == "weekday_list":
                assert all(1 <= d <= 7 for d in value), case["say"]
            elif slot.type == "visit_ref":
                assert set(value) <= {"specialty", "doctor", "date", "time"}, case["say"]
            elif slot.type == "digits":
                assert value.isdigit() and len(value) == 16, case["say"]


def test_choice_examples_point_to_options(catalog):
    for group in catalog.choice_examples:
        for case in group["cases"]:
            assert 1 <= case["choice_index"] <= len(group["options"]), case["say"]


def test_unknown_mode_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["intents"]["I-01"]["mode"] = "manual"
    with pytest.raises(CatalogError):
        parse_catalog(broken)


def test_latin_scenario_code_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["intents"]["I-01"]["scenarios"] = ["C-01"]
    with pytest.raises(CatalogError):
        parse_catalog(broken)


def test_required_slot_must_be_listed(raw):
    broken = copy.deepcopy(raw)
    broken["intents"]["I-03"]["required"] = ["specialty"]
    with pytest.raises(CatalogError):
        parse_catalog(broken)


def test_operator_mode_needs_route(raw):
    broken = copy.deepcopy(raw)
    del broken["intents"]["I-12"]["route"]
    with pytest.raises(CatalogError):
        parse_catalog(broken)
