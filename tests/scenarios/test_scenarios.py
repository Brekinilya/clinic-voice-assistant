import re
from datetime import datetime

import pytest
from scenario_plan import (
    BLOCKING,
    CONTROL_IDS,
    END,
    README,
    STAND_TIME,
    START,
    coverage_markdown,
    doctor_surnames,
    load_fixtures,
    load_scenarios,
    to_option,
)

from app.contracts import NluRequest
from app.nlu.catalog import load_catalog
from app.nlu.parsers import parse_expected
from app.nlu.service import RulesNlu
from app.policies.safety import check
from app.voice.phrases import load_phrasebook
from tests.scenarios.known_issues import checked_known_issue, load_known_issues
from tests.scenarios.runner import run_dialog

SCENARIOS = load_scenarios()
DIALOGS = [d for s in SCENARIOS for d in s.dialogs]
FIXTURES = load_fixtures()
PATIENT_STEPS = [(d, step) for d in DIALOGS for step in d.steps if "patient" in step]
PARSE_STEPS = [(d, step) for d, step in PATIENT_STEPS if "parse" in step]
SERVICE_INTENTS = {"yes", "no", "choice", "repeat", "maybe", "opt_out", "unknown"}


def step_ids(items):
    return [f"{dialog.label}: {step['patient'][:40]}" for dialog, step in items]


def says(step) -> list[str]:
    value = step.get("say", [])
    return [value] if isinstance(value, str) else list(value)


def test_all_control_dialogs_present():
    ids = {s.id for s in SCENARIOS}
    assert set(CONTROL_IDS) <= ids
    assert len(ids) == len(SCENARIOS), "id сценариев повторяются"


def test_blocking_marks_as_in_appendix():
    for s in SCENARIOS:
        if s.id in CONTROL_IDS:
            assert s.blocking == (s.id in BLOCKING), s.id


@pytest.mark.parametrize("dialog", DIALOGS, ids=lambda d: d.label)
def test_references(dialog):
    templates = load_phrasebook().templates
    intents = set(load_catalog().intents) | SERVICE_INTENTS
    datetime.strptime(dialog.setup["now"], STAND_TIME)
    assert dialog.setup.get("channel") in {"inbound", "outbound"}

    for visit in dialog.setup.get("visits", []):
        assert visit["client"] in FIXTURES["clients"] and visit["doctor"] in FIXTURES["doctors"]
        datetime.strptime(visit["at"], STAND_TIME)

    offered = None
    for step in dialog.steps:
        assert "say" in step, f"у шага нет реплики ассистента: {step}"
        for template_id in says(step):
            assert template_id in templates, template_id
        if "intent" in step:
            assert step["intent"] in intents, step["intent"]
        if "transfer" in step:
            assert re.match(r"^R-(0[1-9]|1[0-7])$", step["transfer"]["route"])
            assert step["transfer"]["priority"] in {"P1", "P2", "P3"}
        parse = step.get("parse", {})
        if parse.get("expect") == "choice":
            assert parse["options"] == offered, "варианты в разборе не совпадают с предложенными"
        offered = step.get("options", offered)
        for option in step.get("options", []):
            to_option(option, FIXTURES)


@pytest.mark.parametrize("dialog, step", PATIENT_STEPS, ids=step_ids(PATIENT_STEPS))
def test_patient_phrase_safety(dialog, step):
    """Красные флаги и сигналы на каждой реплике пациента; нет поля safety — флагов быть не должно."""
    verdict = check(step["patient"], operators_on_duty=dialog.operators_on_duty)
    expected = step.get("safety", {})
    assert verdict.flags == set(expected.get("flags", []))
    if "rule" in expected:
        assert verdict.rule == expected["rule"]


@pytest.mark.parametrize("dialog, step", PARSE_STEPS, ids=step_ids(PARSE_STEPS))
def test_patient_phrase_parse(dialog, step):
    expected = step["parse"]
    options = [to_option(o, FIXTURES) for o in expected.get("options", [])]
    parsed = parse_expected(
        step["patient"],
        expected["expect"],
        dialog.now.date(),
        options=options,
        doctors=doctor_surnames(FIXTURES),
    )
    if "intent" in expected:
        assert parsed.intent == expected["intent"]
    for name, value in expected.get("slots", {}).items():
        assert parsed.slots.get(name) == value, name
    if "choice_index" in expected:
        assert parsed.slots.get("choice_index") == expected["choice_index"]


INTENT_STEPS = [(d, step) for d, step in PATIENT_STEPS if "intent" in step]
RULES_NLU = RulesNlu([doctor["full_name"] for doctor in FIXTURES["doctors"].values()])


@pytest.mark.parametrize("dialog, step", INTENT_STEPS, ids=step_ids(INTENT_STEPS))
def test_patient_phrase_intent(dialog, step):
    """Правила либо понимают интент верно, либо молчат там, где это помечено в сценарии."""
    expect = step.get("parse", {}).get("expect", "free")
    request = NluRequest(
        call_id="scenario", turn_id=1, text=step["patient"], expect=expect, today=dialog.now.date()
    )
    intents = RULES_NLU.parse(request).intents
    if step["intent"] == "unknown":
        assert intents == ()
    elif step.get("needs_general_parse"):
        assert not [i for i in intents if i.startswith("I-")]
    else:
        assert step["intent"] in intents


@pytest.mark.parametrize("dialog", DIALOGS, ids=lambda d: d.label)
async def test_dialog_end_to_end(dialog):
    result = await run_dialog(
        dialog, blocking=dialog.scenario in BLOCKING or dialog.scenario == "D-01b"
    )
    reason = checked_known_issue(result)
    if reason is not None:
        pytest.xfail(reason + "; полная приёмка остаётся незелёной: tests.scenarios.run")
    assert result.status == "pass"


def test_known_issues_do_not_include_unknown_dialogs():
    assert set(load_known_issues()) <= {d.label for d in DIALOGS} | {
        "REG-T05 неизвестный пациент → исправление → пятница"
    }


def test_readme_coverage_is_fresh():
    text = README.read_text(encoding="utf-8")
    section = text.split(START, 1)[1].split(END, 1)[0]
    assert section.strip() == coverage_markdown().strip(), (
        "обновите: python -m tests.scenarios.scenario_plan --write"
    )
