"""Матрица контекста и сводка состояния: правила оценки и короткий настоящий прогон."""

from dataclasses import asdict

import pytest

from tests.scenarios import context_matrix
from tests.scenarios.context_matrix import OUTCOMES, execute, load_probes, verdict
from tests.scenarios.scenario_plan import load_scenarios
from tests.scenarios.status import blockers, changes


def test_probe_catalog_is_consistent_with_scenarios():
    data = load_probes()
    labels = {d.label for s in load_scenarios() for d in s.dialogs}
    assert set(data["bases"]) <= labels
    for probe in data["probes"].values():
        assert probe["texts"] and probe["task"]
        assert set(probe["allowed"]) <= set(OUTCOMES)
        for allowed in probe.get("allowed_in", {}).values():
            assert set(allowed) <= set(OUTCOMES)


@pytest.mark.parametrize(
    "phase, result, code, expected",
    [
        ("offered", "answered", None, "ok"),
        ("offered", "not_understood", "NOT_UNDERSTOOD", "gap"),
        ("offered", "ended", "MAYBE", "wrong"),
        ("remind_details", "ended", "MAYBE", "ok"),
        ("offered", "answered", "NO_ACTIVE_VISITS", "wrong"),
    ],
)
def test_verdict_separates_gaps_from_wrong_reactions(phase, result, code, expected):
    probe = {
        "allowed": ["answered"],
        "allowed_in": {"remind_details": ["answered", "ended"]},
        "forbidden_codes": ["NO_ACTIVE_VISITS"],
    }
    assert verdict(probe, phase, result, code) == expected


async def test_matrix_runs_real_dialogues_on_the_emulator():
    data = load_probes()
    cells = await execute(data, bases=["M-01"], probes=["robot", "operator"])
    assert cells and {c.probe for c in cells} == {"robot", "operator"}
    assert {c.outcome for c in cells if c.probe == "operator"} == {"transfer"}
    summary = context_matrix.summary(cells, data)
    assert summary["total"]["n"] == len(cells)
    assert summary["by_probe"]["robot"]["task"] == data["probes"]["robot"]["task"]


def test_matrix_markdown_lists_non_ok_cells_with_their_actual_replies():
    data = load_probes()
    cell = context_matrix.Cell(
        base="M-10 перенос",
        step=4,
        phase="move_offered",
        probe="change_mind",
        text="Я передумал.",
        outcome="not_understood",
        verdict="gap",
        code="NOT_UNDERSTOOD",
        phase_after="move_offered",
        answer="Не поняла, уточните, пожалуйста.",
    )
    document = {
        "source": {"commit": "a" * 40, "dirty": False},
        "summary": context_matrix.summary([cell], data),
        "cells": [asdict(cell)],
    }

    rendered = context_matrix.markdown(document, data)

    assert "## Реплики, требующие доработки" in rendered
    assert "M-10 перенос · move_offered" in rendered
    assert "Я передумал." in rendered
    assert "Не поняла, уточните, пожалуйста." in rendered


def test_status_groups_scenarios_by_blocking_task_and_reports_changes():
    assert blockers({"findings": [{"code": "operator_card_T08"}, {"code": "result_code"}]}) == {
        "T08"
    }
    assert blockers({"findings": [{"code": "result_code"}]}) == {"диалог"}
    previous = {
        "source": {"commit": "a" * 40},
        "t07": {"statuses": {"M-01": "pass", "D-01": "fail"}},
        "context": {"gap_share": 0.6, "by_probe": {"hold": {"ok": 0}}},
    }
    current = {
        "t07": {"statuses": {"M-01": "fail", "D-01": "pass"}},
        "context": {"gap_share": 0.4, "by_probe": {"hold": {"ok": 15}}},
    }
    delta = changes(current, previous)
    assert delta["t07_gained"] == ["D-01"] and delta["t07_lost"] == ["M-01"]
    assert delta["context_ok_delta"] == {"hold": 15}
    assert changes(current, None) == {}
