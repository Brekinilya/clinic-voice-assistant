"""Регламент 3.8, 7.3 и 7.5: граница советов, настояние и приоритеты переводов."""

import asyncio

import pytest
from fixture_bridge import pipeline, turn

from app.contracts import NluResult, SafetyDecision
from app.dialogue.safety_adapter import route_for
from app.mis.budget import MemoryBudget
from app.voice.phrases import load_phrasebook

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"
QUESTION = "Что мне выпить от головной боли?"


def spoken(result):
    return " ".join(result["utterances"])


@pytest.mark.parametrize(
    "routes,priority",
    [
        (["R-01", "R-11"], "P1"),
        (["R-02", "R-03", "R-09", "R-15"], "P2"),
        (
            [
                "R-04",
                "R-05",
                "R-06",
                "R-07",
                "R-08",
                "R-10",
                "R-12",
                "R-13",
                "R-14",
                "R-16",
                "R-17",
            ],
            "P3",
        ),
    ],
)
def test_priorities_follow_regulation_table(routes, priority):
    for route in routes:
        transfer = route_for(SafetyDecision(call_id="test", turn_id=1, rule=route, final=True))
        assert transfer.route == route and transfer.priority == priority
    assert route_for(SafetyDecision(call_id="test", turn_id=1, flags={"medical"})) is None


@pytest.mark.parametrize(
    "text,route,priority",
    [
        ("Сильно болит зуб", "R-09", "P2"),
        ("Я вас плохо слышу", "R-15", "P2"),
        ("Температура сорок", "R-11", "P1"),
        ("Невыносимо болит живот", "R-11", "P1"),
        ("Что мне выпить, не могу дышать", "R-01", "P1"),
    ],
)
async def test_actual_safety_routes_have_regulation_priorities(text, route, priority):
    async with pipeline() as (client, _, emulator, *_):
        result = await turn(client, 1, text)
        assert result["transfer"]["route"] == route
        assert result["transfer"]["priority"] == priority
        assert emulator.write_count == 0


async def test_boundary_and_replay_do_not_count_partials_as_insistence():
    async with pipeline() as (client, service, emulator, *_):
        for text in ("Что выпить", QUESTION):
            result = await turn(client, 1, text, final=False)
            assert result["kind"] == "partial" and result["transfer"] is None
            assert not (await service.store.inspect("scenario")).medical_boundary_pending
        boundary = await turn(client, 1, QUESTION)
        expected = (
            load_phrasebook().get("medical.boundary").text.replace("{specialty:datv}", "терапевту")
        )
        assert boundary["utterances"] == [expected] and boundary["transfer"] is None
        assert await turn(client, 1, QUESTION) == boundary
        repeated = await turn(client, 2, "Повторите, пожалуйста")
        assert repeated["code"] == "MEDICAL_BOUNDARY" and repeated["transfer"] is None
        assert (await service.store.inspect("scenario")).medical_boundary_pending
        partial = await turn(client, 3, "Нет, скажите что выпить", final=False)
        assert partial["kind"] == "partial" and partial["transfer"] is None
        insist = await turn(client, 3, "Нет, вы мне просто скажите, что можно выпить")
        assert insist["transfer"]["route"] == "R-04" and insist["transfer"]["priority"] == "P3"
        assert insist["utterances"] == [load_phrasebook().get("transfer.medical").text]
        assert emulator.write_count == 0


async def test_accepting_medical_offer_starts_normal_booking_with_refinement():
    async with pipeline() as (client, service, emulator, *_):
        assert (await turn(client, 1, QUESTION))["code"] == "MEDICAL_BOUNDARY"
        assert (await turn(client, 2, "Да, но в пятницу"))["phase"] == "identifying"
        assert not (await service.store.inspect("scenario")).medical_boundary_pending
        choices = await turn(client, 3, IDENTITY)
        assert choices["phase"] == "offered" and emulator.write_count == 0
        assert all(
            s.scheduled_at.isoweekday() == 5
            for s in (await service.store.inspect("scenario")).options
        )
        assert (await turn(client, 4, "Давайте первый"))["phase"] == "held"
        assert (await turn(client, 5, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


async def test_declining_medical_offer_ends_call_without_transfer():
    async with pipeline() as (client, _, emulator, *_):
        await turn(client, 1, QUESTION)
        result = await turn(client, 2, "Нет, спасибо")
        assert result["code"] == "CALL_ENDED" and result["transfer"] is None
        assert emulator.write_count == 0


async def test_medical_question_revokes_old_hold_and_yes_does_not_confirm_it():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        held = await turn(client, 3, "Давайте первый")
        partial = await turn(client, 4, QUESTION, final=False)
        assert partial["kind"] == "partial" and partial["transfer"] is None
        result = await turn(client, 4, QUESTION)
        assert result["code"] == "MEDICAL_BOUNDARY" and result["proposal"] is None
        action = await service.booking.store.get(held["proposal"]["action_id"])
        assert action.state == "cancelled"
        assert (await turn(client, 5, "Да"))["phase"] == "offered"
        assert emulator.write_count == 0


async def test_medical_boundary_is_available_while_mis_budget_is_exhausted():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock, limit=1)
        await budget.reserve()
        service.booking.mis._budget = budget
        # Понимание локальное: просьба принята и без справочника МИС, дальше нужен пациент.
        first = await turn(client, 1, "Запишите к терапевту")
        assert first["phase"] == "identifying" and first["code"] != "WAIT_MIS"
        result = await turn(client, 2, QUESTION)
        assert result["code"] == "MEDICAL_BOUNDARY" and result["transfer"] is None
        insist = await turn(client, 3, "Нет, скажите что мне выпить")
        assert insist["transfer"]["route"] == "R-04" and emulator.write_count == 0


async def test_medical_flag_from_nlu_also_gets_boundary_before_handoff():
    async with pipeline() as (client, service, emulator, *_):

        class MedicalInput:
            async def parse(self, state, received):
                return NluResult(
                    call_id=state.call_id,
                    turn_id=received.turn_id,
                    flags={"medical"},
                    source="rules",
                )

        service.conversation.input = MedicalInput()
        result = await turn(client, 1, "Поясните моё состояние")
        assert result["code"] == "MEDICAL_BOUNDARY" and result["transfer"] is None
        result = await turn(client, 2, "Мне всё-таки нужен совет")
        assert result["transfer"]["route"] == "R-04" and emulator.write_count == 0


@pytest.mark.parametrize(
    "phrase",
    [
        "Побаливает живот",
        "Нужно ли вызывать врача, если температура?",
    ],
)
async def test_unspecified_symptoms_get_boundary_and_booking_offer(phrase):
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, phrase)
        assert result["code"] == "MEDICAL_BOUNDARY", result
        assert result["transfer"] is None
        assert "медицинские советы" in spoken(result).casefold()
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.medical_boundary_pending
        assert emulator.log == [] and emulator.write_count == 0


@pytest.mark.parametrize("phrase", ["Я умираю", "Мне очень плохо"])
async def test_unspecified_distress_without_a_red_flag_gets_the_medical_boundary(phrase):
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, phrase)
        assert result["code"] == "MEDICAL_BOUNDARY", result
        assert result["transfer"] is None
        assert "медицинские советы" in spoken(result).casefold()
        assert (await service.store.inspect("scenario")).client_id is None
        assert emulator.write_count == emulator.patch_count == 0


async def test_explicit_life_threat_goes_to_emergency_route_without_screening_question():
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, "Мне трудно дышать")
        assert result["transfer"]["route"] == "R-01" and result["transfer"]["priority"] == "P1"
        assert "угроз" not in spoken(result).casefold()
        assert emulator.write_count == emulator.patch_count == 0


async def test_generic_abdominal_pain_gets_boundary_without_self_assessment():
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, "У меня болит живот")
        assert result["code"] == "MEDICAL_BOUNDARY"
        assert result["transfer"] is None
        assert "медицинские советы" in spoken(result).casefold()
        assert (await service.store.inspect("scenario")).client_id is None
        assert emulator.write_count == 0


async def test_red_b_on_a_configured_holiday_uses_the_emergency_route():
    async with pipeline({"now": "04.11.2026 10:15"}) as (client, _, emulator, *_):
        result = await turn(client, 1, "Температура сорок")
        assert result["transfer"]["route"] == "R-01", result
        assert result["transfer"]["priority"] == "P1", result
        assert "сто три" in spoken(result).casefold()
        assert emulator.write_count == emulator.patch_count == 0


async def test_explicit_booking_with_symptoms_keeps_booking_context():
    async with pipeline() as (client, service, emulator, *_):
        first = await turn(client, 1, "У меня болит горло, запишите к терапевту")
        assert first["transfer"] is None, first
        assert first["phase"] == "identifying", first
        state = await service.store.inspect("scenario")
        assert state.purpose == "booking" and state.specialty == "терапевт"
        assert state.client_id is None and emulator.write_count == 0


async def test_nonurgent_symptom_during_booking_resumes_current_question():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        result = await turn(client, 3, "Болит голова")
        assert result["transfer"] is None and result["code"] == "BOOKING_RESUMED", result
        state = await service.store.inspect("scenario")
        assert state.phase.value == "offered"
        assert state.purpose == "booking" and state.specialty == "терапевт"
        assert state.client_id is not None and emulator.write_count == emulator.patch_count == 0


async def test_repeating_a_non_red_flag_symptom_does_not_count_as_insistence():
    async with pipeline() as (client, _, emulator, *_):
        first = await turn(client, 1, "У меня болит живот")
        second = await turn(client, 2, "У меня всё ещё болит живот")
        assert first["code"] == second["code"] == "MEDICAL_BOUNDARY"
        assert second["transfer"] is None
        assert emulator.write_count == emulator.patch_count == 0


async def test_medical_partial_during_preflight_prevents_post():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        await turn(client, 3, "Давайте первый")
        ready, finish = asyncio.Event(), asyncio.Event()
        original = service.booking._preflight

        async def delayed(*args, **kwargs):
            ready.set()
            await finish.wait()
            return await original(*args, **kwargs)

        service.booking._preflight = delayed
        task = asyncio.create_task(turn(client, 4, "Да"))
        await ready.wait()
        assert (await turn(client, 5, QUESTION, final=False))["kind"] == "partial"
        finish.set()
        assert (await task)["code"] == "TURN_INTERRUPTED" and emulator.write_count == 0
        assert (await turn(client, 5, QUESTION))["code"] == "MEDICAL_BOUNDARY"


@pytest.mark.parametrize(
    "answer,route", [("Да", "R-08"), ("Нет, спасибо", "R-08"), (QUESTION, "R-04")]
)
async def test_unknown_write_survives_medical_boundary_and_next_answer(answer, route):
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        held = await turn(client, 3, "Давайте первый")
        emulator.next_failure = "lost_response"
        assert (await turn(client, 4, "Да"))["code"] == "RECONCILING"
        boundary = await turn(client, 5, QUESTION)
        assert boundary["code"] == "MEDICAL_BOUNDARY" and boundary["phase"] == "reconciling"
        assert boundary["proposal"] == held["proposal"]
        accepted = await turn(client, 6, answer)
        assert accepted["transfer"]["route"] == route
        assert accepted["transfer"]["action_id"] == held["proposal"]["action_id"]
        assert emulator.write_count == 1
