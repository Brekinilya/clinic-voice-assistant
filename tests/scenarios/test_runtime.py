"""Проверяем изоляцию и измерения runner, а не подгоняем ответы приложения."""

import asyncio
import json
from collections import deque
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.mis.models import VisitStatus
from tests.scenarios.known_issues import checked_known_issue
from tests.scenarios.regressions import quota_regression
from tests.scenarios.run import accepted, execute_suite, markdown, report
from tests.scenarios.runner import DialogResult, run_dialog, verify_records
from tests.scenarios.runtime import TOKEN, ScenarioRuntime
from tests.scenarios.scenario_plan import HERE, load_scenarios


@pytest.fixture
def runtime():
    return ScenarioRuntime(next(s for s in load_scenarios() if s.id == "M-01").dialogs[0])


@pytest.fixture
async def client(runtime):
    async with httpx.AsyncClient(
        base_url="https://scenario.invalid/api/v1/",
        headers={"Authorization": "Bearer " + TOKEN},
        transport=runtime.emulator.transport(),
    ) as http:
        yield http


async def test_escalations_match_public_schema_and_do_not_deduplicate(client, runtime):
    contract = json.loads((HERE / "mis_contract.json").read_text(encoding="utf-8"))["schemas"]
    payload = {"client_id": 1, "description": "Вымышленная заявка\nПроверка передачи"}
    first = await client.post("escalations", json=payload)
    second = await client.post("escalations", json=payload)
    assert first.status_code == second.status_code == 201
    assert set(first.json()) == set(contract["Escalation"]["required"])
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["description"] == payload["description"]
    page = await client.get("escalations", params={"limit": 1, "offset": 1, "client_id": 1})
    assert page.json() == [first.json()]
    assert (await client.get("escalations/" + str(first.json()["id"]))).json() == first.json()
    assert (await client.patch("escalations/1", json={})).status_code == 404
    assert (await client.delete("escalations/1")).status_code == 404
    assert runtime.emulator.escalation_count == 2


@pytest.mark.parametrize("description", ["", " ", "x" * 1001, "текст\x00", "текст\v", 123, None])
async def test_invalid_escalation_never_changes_stand(client, runtime, description):
    assert (
        await client.post("escalations", json={"client_id": 1, "description": description})
    ).status_code == 400
    assert runtime.emulator.escalation_count == 0


@pytest.mark.parametrize("identifier", [True, 0, -1, 2147483648, 999, "1"])
async def test_invalid_or_foreign_escalation_client(client, identifier):
    assert (
        await client.post("escalations", json={"client_id": identifier, "description": "Тест"})
    ).status_code == 400


async def test_team_isolation_for_escalations_and_client_patch(client, runtime):
    emulator = runtime.emulator
    emulator.clients["other"] = {}
    emulator.escalations["other"] = {}
    emulator.windows["other"] = deque()
    created = await client.post("escalations", json={"client_id": 1, "description": "Тест"})
    headers = {"Authorization": "Bearer other"}
    assert (
        await client.get("escalations/" + str(created.json()["id"]), headers=headers)
    ).status_code == 404
    assert (await client.get("escalations", headers=headers)).json() == []
    assert (
        await client.patch("clients/1", json={"comment": "Тест"}, headers=headers)
    ).status_code == 404
    assert (
        await client.get("escalations", headers={"Authorization": "Bearer invalid"})
    ).status_code == 401


async def test_patch_client_preserves_unspecified_fields_and_clears_null(client):
    before = (await client.get("clients/1")).json()
    changed = (
        await client.patch("clients/1", json={"comment": "Исходный текст\nПодтвердил визит"})
    ).json()
    assert changed | {"comment": before["comment"]} == before
    assert changed["comment"].endswith("Подтвердил визит")
    normalized = (
        await client.patch("clients/1", json={"middle_name": " " * 150 + "  Викторовна\n "})
    ).json()
    assert normalized["middle_name"] == "Викторовна"
    cleared = (await client.patch("clients/1", json={"middle_name": None, "comment": "  "})).json()
    assert cleared["middle_name"] is cleared["comment"] is None
    assert (await client.patch("clients/1", json={"unknown": "ignored"})).json() == cleared


@pytest.mark.parametrize(
    "payload",
    [
        {"last_name": "Другая"},
        {"first_name": None},
        {"birth_date": None},
        {"comment": "x" * 1001},
        {"comment": "\v"},
        {"comment": 7},
        [],
    ],
)
async def test_invalid_patch_is_atomic(client, runtime, payload):
    before = (await client.get("clients/1")).json()
    assert (await client.patch("clients/1", json=payload)).status_code == 400
    assert (await client.get("clients/1")).json() == before
    assert runtime.emulator.client_patch_count == 0


async def test_server_429_retry_after_and_window_without_sleep(runtime):
    runtime.emulator.limit = 1
    async with httpx.AsyncClient(
        base_url="https://scenario.invalid/api/v1/",
        headers={"Authorization": "Bearer " + TOKEN},
        transport=runtime.emulator.transport(),
    ) as http:
        assert (await http.get("doctors")).status_code == 200
        denied = await http.get("doctors")
        assert denied.status_code == 429 and denied.headers["Retry-After"] == "60"
        runtime.clock.value += timedelta(seconds=60)
        assert (await http.get("doctors")).status_code == 200


async def test_a_new_runtime_cannot_see_another_dialog(runtime):
    other = ScenarioRuntime(next(s for s in load_scenarios() if s.id == "M-01").dialogs[0])
    result = await run_dialog(
        next(s for s in load_scenarios() if s.id == "M-01").dialogs[0], runtime=runtime
    )
    assert result.status == "pass" and result.mutations["visits_created"] == 1
    assert other.emulator.records[TOKEN] == {} and not other.booking_store.actions
    assert all(v.status == VisitStatus.PLANNED for v in runtime.emulator.records[TOKEN].values())


async def test_quota_regression_is_measured_and_not_accepted():
    dialog, runtime = quota_regression()
    result = await run_dialog(dialog, runtime=runtime)
    assert result.budget_profile == "memory_8_server_10_per_min"
    assert len(runtime.emulator.doctors) == 85
    assert result.mutations["visits_created"] == 0
    summary = json.dumps(result.requests)
    assert TOKEN not in summary and "Authorization" not in summary
    reason = checked_known_issue(result)
    if reason:
        pytest.xfail(reason)


def test_known_issue_never_masks_new_errors_or_false_positive():
    result = DialogResult("x", "неизвестный сценарий", False)
    result.add("unexpected", "новое расхождение")
    with pytest.raises(AssertionError, match="Новое расхождение"):
        checked_known_issue(result.finish())
    result.add("unsafe", "неправомерная запись", "critical")
    with pytest.raises(AssertionError, match="Нарушение безопасности"):
        checked_known_issue(result.finish())


def test_record_verification_detects_missing_visit(runtime):
    expected = {
        "created": [{"client": "smirnova_ov", "doctor": "kuznetsova", "at": "02.10.2026 15:15"}]
    }
    result = DialogResult("x", "x", False)
    verify_records(result, runtime, expected)
    assert {"created_count", "created_visit"} <= {f.code for f in result.findings}


async def test_lost_post_response_never_creates_a_second_visit():
    original = next(s for s in load_scenarios() if s.id == "M-01").dialogs[0]
    dialog = replace(
        original,
        setup=original.setup
        | {"faults": [{"call": "create_visit", "kind": "lost_response", "delay_s": 6, "times": 1}]},
    )
    runtime = ScenarioRuntime(dialog)
    result = await run_dialog(dialog, runtime=runtime)
    assert runtime.emulator.write_count == 1
    assert result.requests["by_method_resource"]["POST /visits"] == 1
    assert any(step["phase"] == "reconciling" for step in result.steps)
    assert result.status != "pass"


async def test_new_input_revokes_http_confirmation_before_post(runtime):
    async with runtime.application() as http:
        for turn, text in enumerate(
            ("Запишите к терапевту", "Смирнова Ольга Викторовна, 12.03.1978", "Первый"), 1
        ):
            response = await http.post(
                "/api/dialog/scenario/turn", json={"turn_id": turn, "text": text}
            )
            assert response.status_code == 200
        assert response.json()["phase"] == "held"
        started, release = asyncio.Event(), asyncio.Event()
        original = runtime.service.booking.mis.doctors

        async def slow(*args, **kwargs):
            if kwargs.get("fresh"):
                started.set()
                await release.wait()
            return await original(*args, **kwargs)

        runtime.service.booking.mis.doctors = slow
        task = asyncio.create_task(
            http.post("/api/dialog/scenario/turn", json={"turn_id": 4, "text": "Да"})
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            interrupted = await http.post("/api/dialog/scenario/input-started", json={"turn_id": 5})
            assert interrupted.status_code == 200
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
            runtime.service.booking.mis.doctors = original
        assert runtime.emulator.write_count == 0
        assert not any(
            r.method == "POST" and r.resource == "visits" for r in runtime.emulator.requests
        )


async def test_record_verification_detects_duplicate_visit(runtime):
    dialog = next(s for s in load_scenarios() if s.id == "M-01").dialogs[0]
    await run_dialog(dialog, runtime=runtime)
    visit = next(iter(runtime.emulator.records[TOKEN].values()))
    runtime.emulator.records[TOKEN][visit.id + 1] = visit.model_copy(update={"id": visit.id + 1})
    result = DialogResult("x", "x", False)
    verify_records(
        result,
        runtime,
        {
            "at_most_one": {
                "client": "smirnova_ov",
                "doctor": "kuznetsova",
                "at": "02.10.2026 15:15",
            }
        },
    )
    assert any(f.code == "duplicate_visit" and f.kind == "critical" for f in result.findings)
    unexpected = DialogResult("x", "x", False)
    verify_records(unexpected, runtime, {"cancelled": []})
    assert any(f.code == "unexpected_create" and f.kind == "critical" for f in unexpected.findings)


async def test_full_report_contains_every_dialog_and_blocks_incomplete_acceptance():
    results = await execute_suite()
    expected_count = sum(len(scenario.dialogs) for scenario in load_scenarios()) + 1
    assert len(results) == expected_count
    assert not any(f.kind in {"error", "critical"} for r in results for f in r.findings)
    assert not any(f.code.startswith("unmapped:") for r in results for f in r.findings)
    document = report(results)
    assert document["source"]["source_sha256"]
    assert len(document["results"]) == expected_count
    assert sum(document["counts"].values()) == expected_count
    assert accepted([replace(r, status="pass", findings=[]) for r in results], "all")
    assert not accepted([replace(r, status="pass", findings=[]) for r in results[:-2]], "all")
    assert not accepted([replace(r, status="pass", findings=[]) for r in results[:-1]], "all")
    assert "HTTP ASGI" in document["scope"] and "unsupported" in markdown(document)
    by_label = {result.label: result for result in results}
    confirmed = by_label["M-10 подтверждение"]
    confirmation = confirmed.evidence["outbound"]
    assert confirmed.status == "pass"
    assert confirmation["attempt_state"] == "finished"
    assert confirmation["mark_state"] == "applied"
    assert confirmation["client_comment_patches"] == 1
    assert confirmation["preserved_existing_comment"]
    assert "ReminderExecutor.tick" in confirmation["path"]

    wrong_person = by_label["D-10"]
    assert wrong_person.status == "unsupported"
    assert [finding.code for finding in wrong_person.findings] == ["sms_delivery_T20"]
    assert wrong_person.evidence["outbound"]["client_comment_patches"] == 0
    assert wrong_person.evidence["outbound"]["sms_pending"] is True
    assert wrong_person.evidence["outbound"]["sms_event_state"] == "pending"

    voicemail = by_label["D-11"]
    assert voicemail.status == "pass"
    assert voicemail.evidence["outbound"]["voicemail_event_port"]
