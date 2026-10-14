"""С-04 через настоящий текстовый API: выбор, подтверждение, журнал и сверка отмены."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
import yaml
from fixture_bridge import SCENARIOS, pipeline, turn

from app.booking.models import Approval, BookingError
from app.cancellation.models import CancelState
from app.mis.budget import MemoryBudget
from app.mis.models import VisitStatus
from app.mis.ports import MisOutcomeUnknown

IDENTITY = "Иванов Сергей Петрович, третье января девяностого"
SETUP = {"visits": [{"client": "ivanov_sp", "doctor": "belova", "at": "02.10.2026 10:40"}]}


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def cancel_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


async def prepare(client):
    await turn(client, 1, "Отмените, пожалуйста, запись к лору")
    offered = await turn(client, 2, IDENTITY)
    assert offered["code"] == "RESCHEDULE_OFFERED"
    result = await turn(client, 3, "Отменить, уже не нужно")
    assert result["code"] == "CANCEL_CONFIRMATION_REQUIRED"
    return result


@pytest.mark.parametrize(
    "answer",
    [
        "Не переносите, просто отмените",
        "Нет, не надо переносить, отменяйте",
        "Не надо переносить",
        "Не перенести, а отменить",
    ],
)
async def test_negated_reschedule_selects_cancel_but_requires_final_yes(cancel_engine, answer):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, _, emulator, *_):
        await turn(client, 1, "Отмените мою запись")
        assert (await turn(client, 2, IDENTITY))["code"] == "RESCHEDULE_OFFERED"
        prepared = await turn(client, 3, answer)
        assert prepared["code"] == "CANCEL_CONFIRMATION_REQUIRED"
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.patch_count == emulator.write_count == 0
        assert (await turn(client, 4, "Да"))["code"] == "CANCELLED"
        assert emulator.patch_count == 1 and emulator.write_count == 0


@pytest.mark.parametrize(
    "answer",
    ["Не отменяйте, лучше перенесите на пятницу", "Не могу прийти, давайте перенесём"],
)
async def test_positive_reschedule_never_prepares_cancellation(cancel_engine, answer):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, _, emulator, *_):
        await turn(client, 1, "Отмените мою запись")
        await turn(client, 2, IDENTITY)
        offered = await turn(client, 3, answer)
        assert offered["code"] != "CANCEL_CONFIRMATION_REQUIRED"
        assert offered["cancel_proposal"] is None
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.patch_count == emulator.write_count == 0


@pytest.mark.parametrize("path", ["main/c04_cancel.yaml", "control/07_cancel_one_of_two.yaml"])
async def test_cancellation_yaml_dialogues(cancel_engine, path):
    case = yaml.safe_load((SCENARIOS / path).read_text(encoding="utf-8"))
    async with pipeline(case["setup"], pg_engine=cancel_engine) as (
        client,
        service,
        emulator,
        _,
        doctors,
        clients,
    ):
        before = dict(emulator.records["demo"])
        number, offers = 0, 0
        for step in case["steps"]:
            if "patient" not in step:
                continue
            number += 1
            result = await turn(client, number, step["patient"])
            offers += result["code"] == "RESCHEDULE_OFFERED"
            if step.get("say") == "cancel.which":
                assert len(result["visits"]) == 2 and result["code"] == "CANCEL_CHOICE_REQUIRED"
            if step.get("say") == "cancel.confirm":
                assert emulator.patch_count == 0 and result["cancel_proposal"] is not None
            for forbidden in step.get("forbid", []):
                assert forbidden not in " ".join(result["utterances"])
        assert result["code"] == "CANCELLED" and offers == 1
        assert emulator.patch_count == 1 and emulator.write_count == 0
        assert await turn(client, number, case["steps"][-1]["patient"]) == result
        for original in before.values():
            expected_cancelled = original.doctor.id == doctors["belova"]
            actual = emulator.records["demo"][original.id]
            assert actual.status == (
                VisitStatus.CANCELLED if expected_cancelled else original.status
            )
            if not expected_cancelled:
                assert actual == original


async def test_guard_and_wire_body_within_shared_budget(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, clock, *_):
        requests = []

        async def capture(request):
            requests.append(request)

        service.booking.mis._client.event_hooks["request"].append(capture)
        service.booking.mis._budget = MemoryBudget(clock)
        prepared = await prepare(client)
        assert (await turn(client, 4, "Да", final=False))["kind"] == "partial"
        assert emulator.patch_count == 0
        result = await turn(client, 4, "Да")
        assert result["code"] == "CANCELLED"
        assert len(requests) <= 8
        patches = [r for r in requests if r.method == "PATCH"]
        assert len(patches) == 1 and patches[0].url.path.endswith("/visits/1")
        assert json.loads(patches[0].content) == {"status": "Отменена"}
        action = await service.conversation.cancellation.cancellation.store.get(
            prepared["cancel_proposal"]["action_id"]
        )
        assert (
            action.state == CancelState.CANCELLED
            and action.reason == "not_needed"
            and not action.late
        )


@pytest.mark.parametrize("failure", ["lost_response", "server_error", "invalid_response"])
async def test_unknown_patch_is_reconciled_without_repeating(cancel_engine, failure):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        emulator.next_failure = failure
        pending = await turn(client, 4, "Да")
        assert (
            pending["code"] == "CANCEL_RECONCILING"
            and pending["cancel_proposal"] == prepared["cancel_proposal"]
        )
        assert await turn(client, 4, "Да") == pending and emulator.patch_count == 1
        result = await turn(client, 5, "Получилось?")
        assert result["code"] == "CANCELLED" and emulator.patch_count == 1


async def test_new_partial_during_fresh_check_prevents_patch(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        await prepare(client)
        started, finish = asyncio.Event(), asyncio.Event()
        original = service.booking.mis.visit

        async def delayed(visit_id):
            started.set()
            await finish.wait()
            return await original(visit_id)

        service.booking.mis.visit = delayed
        task = asyncio.create_task(turn(client, 4, "Да"))
        await started.wait()
        await turn(client, 5, "Нет, другую запись", final=False)
        finish.set()
        assert (await task)["code"] == "TURN_INTERRUPTED"
        assert emulator.patch_count == 0


async def test_cancelled_visit_releases_local_creation_journal(cancel_engine):
    async with pipeline(pg_engine=cancel_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого")
        held = await turn(client, 3, "Давайте первый")
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert (await turn(client, 5, "Отмените мою запись"))["code"] == "RESCHEDULE_OFFERED"
        await turn(client, 6, "Отменить, уже не нужно")
        assert (await turn(client, 7, "Да"))["code"] == "CANCELLED"
        booked = await service.booking.store.get(held["proposal"]["action_id"])
        assert booked.state == "archived" and await service.booking.store.available(
            booked.slot, service.booking.clock.now()
        )
        assert (await turn(client, 8, "На когда я записан?"))["code"] == "NO_ACTIVE_VISITS"
        choices = await turn(client, 9, "Запишите к терапевту на пятницу")
        assert choices["phase"] == "offered"
        assert emulator.write_count == 1 and emulator.patch_count == 1


async def test_red_flag_and_medical_boundary_do_not_cancel(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        result = await turn(client, 4, "Какую таблетку принять?")
        assert result["code"] == "MEDICAL_BOUNDARY" and result["cancel_proposal"] is None
        action = await service.conversation.cancellation.cancellation.store.get(
            prepared["cancel_proposal"]["action_id"]
        )
        assert action.state == CancelState.ABORTED and emulator.patch_count == 0
        assert (await turn(client, 5, "не могу дышать", final=False))["code"] == "EMERGENCY"


async def test_stale_visit_and_missing_server_guard_never_patch(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        cancellation = service.conversation.cancellation.cancellation
        with pytest.raises(BookingError, match="DIALOGUE_GUARD_REQUIRED"):
            await cancellation.confirm(
                Approval(
                    call_id="scenario",
                    action_id=prepared["cancel_proposal"]["action_id"],
                    revision=1,
                    turn_id=4,
                    is_final=True,
                    permitted=True,
                )
            )
        emulator.records["demo"][1] = emulator.records["demo"][1].model_copy(
            update={"scheduled_at": emulator.records["demo"][1].scheduled_at + timedelta(hours=1)}
        )
        result = await turn(client, 4, "Да")
        assert result["code"] == "VISIT_CHANGED" and emulator.patch_count == 0
        refreshed = await turn(client, 5, "Повторите")
        assert refreshed["code"] == "CANCEL_CONFIRMATION_REQUIRED"
        assert refreshed["cancel_proposal"] != prepared["cancel_proposal"]
        assert emulator.patch_count == 0


async def test_quota_before_patch_requires_new_yes_without_extending_deadline(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, clock, *_):
        service.booking.mis._budget = MemoryBudget(clock, limit=5)
        prepared = await prepare(client)
        cancellation = service.conversation.cancellation.cancellation
        waiting = await turn(client, 4, "Да")
        assert waiting["code"] == "WAIT_MIS" and emulator.patch_count == 0
        action = await cancellation.store.get(prepared["cancel_proposal"]["action_id"])
        assert action.state == CancelState.PREPARED and action.confirmed_turn is None
        assert action.expires_at == datetime.fromisoformat(
            prepared["cancel_proposal"]["expires_at"]
        )
        assert (await turn(client, 5, "Да"))["code"] == "WAIT_MIS"
        clock.value += timedelta(seconds=60)
        assert emulator.patch_count == 0
        assert (await turn(client, 6, "Да"))["code"] == "CANCELLED"
        assert emulator.patch_count == 1


async def test_readback_quota_keeps_pending_and_does_not_repeat_patch(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, clock, *_):
        service.booking.mis._budget = MemoryBudget(clock, limit=6)
        await prepare(client)
        assert (await turn(client, 4, "Да"))["code"] == "CANCEL_RECONCILING"
        assert emulator.patch_count == 1
        clock.value += timedelta(seconds=60)
        assert (await turn(client, 5, "Хорошо"))["code"] == "CANCELLED"
        assert emulator.patch_count == 1


async def test_unknown_write_with_still_planned_visit_never_retries(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        await prepare(client)
        attempts = []

        async def unknown(*args):
            attempts.append(args)
            raise MisOutcomeUnknown("TRANSPORT_FAILURE")

        service.booking.mis.change_visit = unknown
        assert (await turn(client, 4, "Да"))["code"] == "CANCEL_RECONCILING"
        handed = await turn(client, 5, "Получилось?")
        assert handed["transfer"]["route"] == "R-06"
        assert handed["transfer"]["action_id"] is not None
        assert len(attempts) == 1 and emulator.patch_count == 0
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED


@pytest.mark.parametrize("now,late", [("02.10.2026 09:15", True), ("02.10.2026 08:40", False)])
async def test_late_cancel_is_marked_without_reproach(cancel_engine, now, late):
    async with pipeline({**SETUP, "now": now}, pg_engine=cancel_engine) as (
        client,
        service,
        emulator,
        *_,
    ):
        prepared = await prepare(client)
        result = await turn(client, 4, "Да")
        assert result["code"] == "CANCELLED" and emulator.patch_count == 1
        action = await service.conversation.cancellation.cancellation.store.get(
            prepared["cancel_proposal"]["action_id"]
        )
        assert action.late is late
        assert "позд" not in " ".join(result["utterances"]).casefold()


async def test_past_visit_is_not_offered_and_no_disclosure_before_identity(cancel_engine):
    async with pipeline({**SETUP, "now": "02.10.2026 10:40"}, pg_engine=cancel_engine) as (
        client,
        _,
        emulator,
        *_,
    ):
        first = await turn(client, 1, "Отмените запись")
        assert first["code"] == "IDENTIFICATION_REQUIRED" and not first["visits"]
        assert "Белова" not in " ".join(first["utterances"])
        assert (await turn(client, 2, IDENTITY))["code"] == "NO_ACTIVE_VISITS"
        assert emulator.patch_count == 0


async def test_final_refusal_aborts_prepared_cancellation(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        assert (await turn(client, 4, "Нет"))["code"] == "CALL_ENDED"
        action = await service.conversation.cancellation.cancellation.store.get(
            prepared["cancel_proposal"]["action_id"]
        )
        assert action.state == CancelState.ABORTED and emulator.patch_count == 0


async def test_concurrent_yes_cannot_cancel_twice(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, _, emulator, *_):
        await prepare(client)
        results = await asyncio.gather(*(turn(client, 4, "Да") for _ in range(5)))
        assert any(r["code"] == "CANCELLED" for r in results)
        assert emulator.patch_count == 1


async def test_unknown_cancellation_survives_medical_question_and_red_flag(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        emulator.next_failure = "lost_response"
        await turn(client, 4, "Да")
        boundary = await turn(client, 5, "Какую таблетку принять?")
        assert boundary["code"] == "MEDICAL_BOUNDARY"
        assert boundary["cancel_proposal"] == prepared["cancel_proposal"]
        handed = await turn(client, 6, "Не могу дышать", final=False)
        assert handed["code"] == "EMERGENCY"
        assert handed["transfer"]["action_id"] == prepared["cancel_proposal"]["action_id"]
        action = await service.conversation.cancellation.cancellation.store.get(
            prepared["cancel_proposal"]["action_id"]
        )
        assert action.state == CancelState.RECONCILING and emulator.patch_count == 1


@pytest.mark.parametrize("change", ["patient", "updated", "completed"])
async def test_external_visit_change_invalidates_confirmation(cancel_engine, change):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, _, emulator, *_):
        await prepare(client)
        before = emulator.records["demo"][1]
        updates = {
            "patient": {"client_id": before.client_id + 1},
            "updated": {"updated_at": before.updated_at + timedelta(minutes=1)},
            "completed": {"status": VisitStatus.COMPLETED},
        }
        emulator.records["demo"][1] = before.model_copy(update=updates[change])
        assert (await turn(client, 4, "Да"))["code"] == "VISIT_CHANGED"
        assert emulator.patch_count == 0


async def test_already_cancelled_in_mis_does_not_need_another_patch(cancel_engine):
    async with pipeline(SETUP, pg_engine=cancel_engine) as (client, _, emulator, *_):
        await prepare(client)
        emulator.records["demo"][1] = emulator.records["demo"][1].model_copy(
            update={"status": VisitStatus.CANCELLED}
        )
        assert (await turn(client, 4, "Да"))["code"] == "CANCELLED"
        assert emulator.patch_count == 0


async def test_cancel_cannot_hide_unknown_creation_in_another_call(cancel_engine):
    async with pipeline(pg_engine=cancel_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого")
        held = await turn(client, 3, "Давайте первый")
        emulator.next_failure = "lost_response"
        assert (await turn(client, 4, "Да"))["code"] == "RECONCILING"
        await turn(client, 1, "Отмените мою запись", call_id="rival")
        offered = await turn(
            client,
            2,
            "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого",
            call_id="rival",
        )
        assert offered["code"] == "RESCHEDULE_OFFERED"
        blocked = await turn(client, 3, "Отменить", call_id="rival")
        assert blocked["transfer"]["route"] == "R-08" and emulator.patch_count == 0
        assert (
            await service.booking.store.get(held["proposal"]["action_id"])
        ).state == "reconciling"
