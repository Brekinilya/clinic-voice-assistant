"""Прогоны через настоящий ASGI-вход, RulesNlu/safety и HTTP-адаптер МИС."""

import asyncio
from datetime import timedelta

import pytest
import yaml
from fixture_bridge import SCENARIOS, pipeline, turn

from app.booking.models import Approval, BookingError
from app.contracts import NluResult
from app.mis.budget import MemoryBudget
from app.mis.models import format_datetime
from app.mis.ports import MisOutcomeUnknown

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"


def cases():
    paths = [
        "main/c01_booking.yaml",
        "main/c05_my_visit.yaml",
        "control/01_red_flag_during_booking.yaml",
        "control/01b_red_flag_at_confirmation.yaml",
        "control/02_medicine_question.yaml",
        "control/06_ent_with_constraints.yaml",
        "control/12_not_understood.yaml",
    ]
    return [yaml.safe_load((SCENARIOS / path).read_text(encoding="utf-8")) for path in paths]


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["id"])
async def test_yaml_scenarios_through_http(case):
    async with pipeline(case.get("setup")) as (
        client,
        service,
        emulator,
        clock,
        doctor_ids,
        client_ids,
    ):
        number = 0
        initial_records = dict(emulator.records["demo"])
        for step in case["steps"]:
            if "patient" not in step:
                continue
            number += 1
            result = await turn(client, number, step["patient"])
            if step.get("say") == "my_visits.one":
                assert result["code"] == "INFO_GIVEN" and len(result["visits"]) == 1
                assert result["visits"][0]["room"] == "118"
                assert "понедельник" in " ".join(result["utterances"])
                assert "Лебедев" in " ".join(result["utterances"])
            if step.get("say") == "medical.boundary":
                assert result["code"] == "MEDICAL_BOUNDARY" and result["transfer"] is None
                assert "Записать вас к терапевту?" in " ".join(result["utterances"])
            if "options" in step:
                assert [
                    format_datetime(s.scheduled_at)
                    for s in (await service.store.inspect("scenario")).options
                ] == step["options"]
            if "transfer" in step:
                assert result["transfer"]["route"] == step["transfer"]["route"]
                assert result["transfer"]["priority"] == step["transfer"]["priority"]
                assert result["transfer"]["status"] == "requested"
            for forbidden in step.get("forbid", []):
                assert forbidden.casefold() not in " ".join(result["utterances"]).casefold()
            if step.get("end"):
                assert result["phase"] == "ended"
        expected = case["result"]["stand"]
        records = list(emulator.records["demo"].values())
        if expected == "unchanged":
            assert emulator.records["demo"] == initial_records and emulator.write_count == 0
        else:
            assert len(records) == len(expected["created"]) == emulator.write_count
            for actual, target in zip(records, expected["created"]):
                assert actual.client_id == client_ids[target["client"]]
                assert actual.doctor.id == doctor_ids[target["doctor"]]
                assert format_datetime(actual.scheduled_at) == target["at"]


async def reserve(client):
    await turn(client, 1, "Запишите к терапевту на пятницу")
    await turn(client, 2, IDENTITY)
    return await turn(client, 3, "В десять тридцать")


async def test_repeat_yes_has_one_write_and_readback_after_lost_response():
    async with pipeline() as (client, service, emulator, clock, *_):
        assert (await reserve(client))["phase"] == "held"
        emulator.next_failure = "lost_response"
        pending = await turn(client, 4, "Да")
        assert pending["code"] == "RECONCILING"
        assert await turn(client, 4, "Да") == pending
        assert emulator.write_count == 1
        clock.value += timedelta(minutes=5)
        result = await turn(client, 5, "На когда я записана?")
        assert result["code"] == "BOOKED" and result["visit_id"] == 1
        assert emulator.write_count == 1


async def test_success_announces_confirmed_doctor_and_time():
    async with pipeline() as (client, service, emulator, *_):
        reserved = await reserve(client)
        result = await turn(client, 4, "Да")
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        spoken = " ".join(result["utterances"])
        assert result["code"] == "BOOKED" and emulator.write_count == 1
        assert "Кузнецова" in spoken and "терапевт" in spoken
        assert "второе октября" in spoken and "десять тридцать" in spoken
        assert emulator.records["demo"][result["visit_id"]].scheduled_at == action.slot.scheduled_at


async def test_natural_booking_uses_eight_requests_with_fresh_narrow_confirmation():
    async with pipeline() as (client, service, emulator, clock, doctor_ids, client_ids):
        requests = []

        async def capture(request):
            requests.append(request)

        service.booking.mis._client.event_hooks["request"].append(capture)
        service.booking.mis._budget = MemoryBudget(clock)
        reserved = await reserve(client)
        assert reserved["phase"] == "held" and len(requests) == 4
        clock.value += timedelta(seconds=20)
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert len(requests) == 8 and emulator.write_count == 1
        assert [r.method for r in requests] == ["GET"] * 7 + ["POST"]
        doctor_check, patient_check = requests[-3:-1]
        assert dict(doctor_check.url.params) == {
            "doctor_id": str(doctor_ids["kuznetsova"]),
            "status": "Запланирована",
            "date_from": "02.10.2026",
            "date_to": "02.10.2026",
            "limit": "50",
            "offset": "0",
        }
        assert dict(patient_check.url.params) == {
            "client_id": str(client_ids["smirnova_ov"]),
            "status": "Запланирована",
            "date_from": "30.09.2026",
            "limit": "50",
            "offset": "0",
        }
        # Сведения о других пациентах и врачах целиком не выгружаются.
        assert all(
            "client_id" in r.url.params or "doctor_id" in r.url.params
            for r in requests
            if r.method == "GET" and r.url.path.endswith("/visits")
        )


async def test_local_budget_before_post_preserves_hold_for_later_confirmation():
    async with pipeline() as (client, service, emulator, clock, *_):
        service.booking.mis._budget = MemoryBudget(clock, limit=7)
        reserved = await reserve(client)
        result = await turn(client, 4, "Да")
        assert result["code"] == "WAIT_MIS" and result["phase"] == "held"
        assert result["proposal"] == reserved["proposal"]
        assert emulator.write_count == 0
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        assert action.state == "held" and action.confirmed_turn is None
        assert (await service.store.inspect("scenario")).authorization is None
        assert await turn(client, 4, "Да") == result
        clock.value += timedelta(seconds=60)
        assert (await turn(client, 5, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


async def test_budget_during_refinement_preserves_preferences_and_releases_old_hold():
    async with pipeline() as (client, service, emulator, clock, *_):
        service.booking.mis._budget = MemoryBudget(clock, limit=5)
        reserved = await reserve(client)
        # Заполняем остаток квоты другим обращением: подбор теперь обходится дешевле.
        await service.booking.mis._budget.reserve()
        result = await turn(client, 4, "Да, но лучше в понедельник")
        assert result["code"] == "WAIT_MIS" and result["phase"] == "identified"
        assert result["proposal"] is None
        assert (
            await service.booking.store.get(reserved["proposal"]["action_id"])
        ).state == "cancelled"
        current = await service.store.inspect("scenario")
        assert current.preferences.date_from.isoweekday() == 1
        clock.value += timedelta(seconds=60)
        result = await turn(client, 5, "Хочу в понедельник")
        assert result["phase"] == "offered" and emulator.write_count == 0


async def test_specialty_alias_does_not_discard_requested_date():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к лору на пятницу")
        before = await service.store.inspect("scenario")
        assert before.preferences.date_from.isoweekday() == 5
        result = await turn(client, 2, "К отоларингологу")
        after = await service.store.inspect("scenario")
        assert result["phase"] == "identifying"
        assert after.preferences == before.preferences and emulator.write_count == 0


async def test_yes_with_correction_never_confirms_previous_day():
    async with pipeline() as (client, service, emulator, *_):
        first = await reserve(client)
        changed = await turn(client, 4, "Да, но лучше в понедельник")
        assert changed["phase"] == "offered" and changed["proposal"] is None
        assert emulator.write_count == 0
        assert first["proposal"]["slot"]["scheduled_at"] != changed["options"][0]["scheduled_at"]


@pytest.mark.parametrize("response", ["Нет", "Мне бы записаться к терапевту"])
async def test_new_request_after_unknown_write_retains_action_for_operator(response):
    async with pipeline() as (client, service, emulator, *_):
        reserved = await reserve(client)
        emulator.next_failure = "lost_response"
        assert (await turn(client, 4, "Да"))["code"] == "RECONCILING"
        result = await turn(client, 5, response)
        assert result["phase"] == "handoff"
        assert result["transfer"]["action_id"] == reserved["proposal"]["action_id"]
        assert "снята" not in " ".join(result["utterances"])
        assert emulator.write_count == 1
        assert (
            await service.booking.store.get(result["transfer"]["action_id"])
        ).state == "reconciling"


@pytest.mark.parametrize("failure", ["exception", "wrong_turn"])
async def test_failed_safety_closes_automatic_booking(failure):
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        original = service.safety.evaluate

        def broken(call_id, received):
            if failure == "exception":
                raise RuntimeError("неисправный проверяющий компонент")
            return original(call_id, received).model_copy(update={"turn_id": 99})

        service.safety.evaluate = broken
        result = await turn(client, 4, "Да")
        assert result["code"] == "SAFETY_UNAVAILABLE" and result["phase"] == "handoff"
        assert emulator.write_count == 0


@pytest.mark.parametrize("danger", ["ой, не могу дышать", "Соедините с оператором"])
async def test_partial_danger_preempts_confirmation(danger):
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        result = await turn(client, 4, danger, final=False)
        assert result["phase"] == "handoff"
        assert (await turn(client, 4, "Да"))["phase"] == "handoff"
        assert emulator.write_count == 0


@pytest.mark.parametrize(
    "new_text,expected",
    [("Нет, другое время", "TURN_INTERRUPTED"), ("не могу дышать", "EMERGENCY")],
)
async def test_new_input_during_preflight_revokes_confirmation(new_text, expected):
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        started, finish = asyncio.Event(), asyncio.Event()
        original = service.booking._preflight

        async def delayed(*args, **kwargs):
            started.set()
            await finish.wait()
            return await original(*args, **kwargs)

        service.booking._preflight = delayed
        task = asyncio.create_task(turn(client, 4, "Да"))
        await started.wait()
        await turn(client, 5, new_text, final=False)
        finish.set()
        result = await task
        assert result["code"] == expected
        assert emulator.write_count == 0


async def test_unknown_birth_century_is_not_chosen_or_stored():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        result = await turn(client, 2, "Смирнова Алиса Дмитриевна, седьмое апреля двадцатого")
        assert result["code"] == "BIRTH_DATE_AMBIGUOUS"
        assert (await service.store.inspect("scenario")).client_id is None
        assert emulator.write_count == 0


async def test_operator_and_medical_routes_are_server_decisions():
    async with pipeline() as (client, _, emulator, *_):
        result = await turn(client, 1, "Какую таблетку от давления выпить?")
        assert result["code"] == "MEDICAL_BOUNDARY" and result["transfer"] is None
        result = await turn(client, 2, "Нет, вы мне просто скажите, что можно выпить")
        assert result["transfer"]["route"] == "R-04"
        assert result["transfer"]["status"] == "requested" and emulator.write_count == 0


async def test_default_natural_factory_writes_only_to_emulator():
    import httpx

    from app.demo import DemoClock
    from app.main import create_app
    from app.settings import Settings

    app = create_app(
        Settings(_env_file=None, backend_demo_enabled=True, dialogue_mode="natural"),
        clock=DemoClock(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            await turn(client, 1, "Запишите к терапевту")
            await turn(client, 2, "Тестов Александр Примерович, 01.01.1990")
            await turn(client, 3, "Давайте первый")
            assert (await turn(client, 4, "Да"))["code"] == "BOOKED"


async def test_stale_nlu_result_never_authorizes_write():
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)

        class WrongResult:
            async def parse(self, state, received):
                return NluResult(
                    call_id="other-call", turn_id=received.turn_id, intents=("yes",), source="rules"
                )

        service.conversation.input = WrongResult()
        result = await turn(client, 4, "Да")
        assert result["code"] == "PROCESSING_INTERRUPTED" and emulator.write_count == 0


async def test_danger_after_post_keeps_unknown_operation_for_operator():
    async with pipeline() as (client, service, emulator, *_):
        reserved = await reserve(client)
        sent, finish = asyncio.Event(), asyncio.Event()
        original = service.booking.mis.create_visit

        async def answer_lost(request):
            await original(request)
            sent.set()
            await finish.wait()
            raise MisOutcomeUnknown("LOST_RESPONSE")

        service.booking.mis.create_visit = answer_lost
        task = asyncio.create_task(turn(client, 4, "Да"))
        await sent.wait()
        result = await turn(client, 5, "не могу дышать", final=False)
        assert result["transfer"]["action_id"] == reserved["proposal"]["action_id"]
        finish.set()
        assert (await task)["code"] == "EMERGENCY"
        assert emulator.write_count == 1
        assert (
            await service.booking.store.get(result["transfer"]["action_id"])
        ).state == "reconciling"


async def test_partial_yes_does_not_write_until_final_yes():
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        assert (await turn(client, 4, "Да", final=False))["kind"] == "partial"
        assert emulator.write_count == 0
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


async def test_booking_without_server_dialogue_stamp_is_rejected():
    async with pipeline() as (client, service, emulator, *_):
        reserved = await reserve(client)
        with pytest.raises(BookingError, match="DIALOGUE_GUARD_REQUIRED"):
            await service.booking.confirm(
                Approval(
                    call_id="scenario",
                    action_id=reserved["proposal"]["action_id"],
                    revision=reserved["proposal"]["revision"],
                    turn_id=4,
                    is_final=True,
                    permitted=True,
                )
            )
        assert emulator.write_count == 0
