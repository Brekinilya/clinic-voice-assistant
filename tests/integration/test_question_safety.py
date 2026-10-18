"""Смена личности и служебный вопрос не сохраняют согласие на чужое действие."""

from datetime import date, timedelta

import pytest
from fixture_bridge import pipeline, turn
from test_budget_dialogue import reserve
from test_cancellation import SETUP as CANCEL_SETUP
from test_cancellation import prepare as prepare_cancel
from test_rescheduling import SETUP as MOVE_SETUP
from test_rescheduling import prepare as prepare_move

from app.contracts import NluResult, NluSlots
from app.dialogue.expectation import active_question
from app.dialogue.models import Turn
from app.mis.ports import BudgetExhausted, MisUnavailable
from app.storage.dialogue import PostgresDialogueStore


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def question_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


def inject_identity(service, number, *, intents=(), **fields):
    """Изолируем обработчик от пока не расширенного извлечения ФИО в HELD."""
    actual = service.conversation.input.parse

    async def parse(state, event):
        if event.turn_id == number:
            return NluResult(
                call_id=state.call_id,
                turn_id=event.turn_id,
                intents=intents,
                slots=NluSlots(**fields),
                evidence={key: event.text for key in fields},
                source="rules",
            )
        return await actual(state, event)

    service.conversation.input.parse = parse


@pytest.mark.parametrize("failure", ["not_found", "rate_limit", "unavailable"])
async def test_failed_new_identity_cannot_book_the_previous_patient(question_engine, failure):
    async with pipeline(pg_engine=question_engine) as (client, service, emulator, *_):
        held = await reserve(client)
        inject_identity(
            service,
            4,
            last_name="Сидоров",
            first_name="Павел",
            middle_name="Иванович",
            birth_date=date(1980, 1, 1),
        )
        if failure != "not_found":

            async def unavailable(*args, **kwargs):
                if failure == "rate_limit":
                    raise BudgetExhausted("RATE_LIMIT", retry_after=20)
                raise MisUnavailable("UNAVAILABLE")

            service.booking.identify = unavailable
        response = await turn(client, 4, "Сидоров Павел Иванович, 01.01.1980")
        assert (
            response["code"]
            == {
                "not_found": "IDENTIFICATION_REQUIRED",
                "rate_limit": "WAIT_MIS",
                "unavailable": "MIS_UNAVAILABLE",
            }[failure]
        )
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.phase == "identifying"
        assert state.proposal is None and state.authorization is None and not state.options
        assert (await service.booking.store.get(held["proposal"]["action_id"])).state != "held"
        await turn(client, 5, "Да")
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("mode", ["cancel", "move"])
async def test_new_identity_revokes_cancel_and_move_proposals(question_engine, mode):
    setup, prepare, next_turn = (
        (CANCEL_SETUP, prepare_cancel, 4) if mode == "cancel" else (MOVE_SETUP, prepare_move, 5)
    )
    async with pipeline(setup, pg_engine=question_engine) as (client, service, emulator, *_):
        await prepare(client)
        inject_identity(
            service,
            next_turn,
            last_name="Сидоров",
            first_name="Павел",
            middle_name="Иванович",
            birth_date=date(1980, 1, 1),
        )
        result = await turn(client, next_turn, "Сидоров Павел Иванович, 01.01.1980")
        assert result["code"] == "IDENTIFICATION_REQUIRED"
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.phase == "identifying"
        assert state.cancel_proposal is state.move_proposal is state.proposal is None
        assert state.cancel_target is state.move_target is None
        assert not state.visit_items and state.authorization is None
        await turn(client, next_turn + 1, "Да")
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("question", ["Не записывайте разговор", "Вы робот?"])
@pytest.mark.parametrize("mode", ["booking", "cancel", "move"])
async def test_service_question_repeats_the_actual_confirmation(question_engine, question, mode):
    setup, prepare, next_turn = {
        "booking": (None, reserve, 4),
        "cancel": (CANCEL_SETUP, prepare_cancel, 4),
        "move": (MOVE_SETUP, prepare_move, 5),
    }[mode]
    async with pipeline(setup, pg_engine=question_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        before = await service.store.inspect("scenario")
        mark = len(emulator.log)
        response = await turn(client, next_turn, question)
        after = await service.store.inspect("scenario")
        assert response["phase"] == prepared["phase"]
        assert response["question"] == prepared["question"]
        assert after.proposal == before.proposal
        assert after.cancel_proposal == before.cancel_proposal
        assert after.move_proposal == before.move_proposal
        assert after.authorization is None
        assert tuple(response["utterances"][-len(prepared["utterances"]) :]) == tuple(
            prepared["utterances"]
        )
        assert "Продолжим запись" not in " ".join(response["utterances"])
        assert emulator.log[mark:] == []
        assert emulator.write_count == emulator.patch_count == 0
        result = await turn(client, next_turn + 1, "Да")
        assert (
            result["code"]
            == {
                "booking": "BOOKED",
                "cancel": "CANCELLED",
                "move": "RESCHEDULED",
            }[mode]
        )
        assert emulator.write_count == (mode != "cancel")
        assert emulator.patch_count == (mode != "booking")


async def test_explicit_null_identity_fields_do_not_replace_the_patient(question_engine):
    async with pipeline(pg_engine=question_engine) as (client, service, emulator, *_):
        await reserve(client)
        inject_identity(
            service,
            4,
            intents=("yes",),
            last_name=None,
            first_name=None,
            middle_name=None,
            birth_date=None,
        )
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1
        assert (await service.store.inspect("scenario")).client_id == 1


async def test_service_question_does_not_extend_the_confirmation_deadline(question_engine):
    async with pipeline(pg_engine=question_engine) as (client, service, emulator, clock, *_):
        held = await reserve(client)
        clock.value += timedelta(seconds=89)
        result = await turn(client, 4, "Вы робот?")
        assert result["proposal"]["expires_at"] == held["proposal"]["expires_at"]
        clock.value += timedelta(seconds=2)
        assert (await turn(client, 5, "Да"))["code"] == "PROPOSAL_EXPIRED"
        assert emulator.write_count == 0


async def test_new_existing_patient_gets_a_fresh_context_without_an_automatic_booking(
    question_engine,
):
    async with pipeline(pg_engine=question_engine) as (client, service, emulator, *_):
        old = await reserve(client)
        inject_identity(
            service,
            4,
            last_name="Петров",
            first_name="Игорь",
            middle_name="Николаевич",
            birth_date=date(1962, 5, 5),
        )
        result = await turn(client, 4, "Петров Игорь Николаевич, 05.05.1962")
        state = await service.store.inspect("scenario")
        assert state.client_id == 3
        assert state.proposal is None or state.proposal.action_id != old["proposal"]["action_id"]
        assert result["code"] != "BOOKED"
        assert emulator.write_count == 0


@pytest.mark.parametrize("mode", ["booking", "cancel", "move"])
async def test_new_patient_never_erases_an_already_sent_operation(question_engine, mode):
    setup, prepare, next_turn, proposal_key = {
        "booking": (None, reserve, 4, "proposal"),
        "cancel": (CANCEL_SETUP, prepare_cancel, 4, "cancel_proposal"),
        "move": (MOVE_SETUP, prepare_move, 5, "move_proposal"),
    }[mode]
    async with pipeline(setup, pg_engine=question_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        emulator.next_failure = "lost_response"
        await turn(client, next_turn, "Да")
        assert (await service.store.inspect("scenario")).phase in {
            "reconciling",
            "cancel_reconciling",
            "move_pending",
        }
        stores = {
            "booking": service.booking.store,
            "cancel": service.conversation.cancellation.cancellation.store,
            "move": service.conversation.rescheduling.service.store,
        }
        action_id = prepared[proposal_key]["action_id"]
        before = await stores[mode].get(action_id)
        inject_identity(
            service,
            next_turn + 1,
            last_name="Сидоров",
            first_name="Павел",
            middle_name="Иванович",
            birth_date=date(1980, 1, 1),
        )
        mark = len(emulator.log)
        result = await turn(client, next_turn + 1, "Теперь Сидоров Павел Иванович, 01.01.1980")
        assert result["transfer"]["route"] == "R-16"
        assert await stores[mode].get(action_id) == before
        await turn(client, next_turn + 2, "Да")
        assert emulator.log[mark:] == []


@pytest.mark.postgres
async def test_restart_keeps_question_metadata_and_invalidated_identity(pg_engine):
    async with pipeline(pg_engine=pg_engine) as (client, service, emulator, *_):
        held = await reserve(client)
        restarted = PostgresDialogueStore(pg_engine)
        state = await restarted.inspect("scenario")
        assert active_question(state).model_dump(mode="json") == held["question"]
        cached = await restarted.claim(
            "scenario", 3, service._digest("scenario", Turn(turn_id=3, text="В десять тридцать"))
        )
        assert cached.cached.question == active_question(state)
        inject_identity(
            service,
            4,
            last_name="Сидоров",
            first_name="Павел",
            middle_name="Иванович",
            birth_date=date(1980, 1, 1),
        )
        await turn(client, 4, "Сидоров Павел Иванович, 01.01.1980")
        # Новый экземпляр читает JSON из БД, а не ссылку на прежний Python-объект.
        service.store = PostgresDialogueStore(pg_engine)
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.proposal is None
        assert active_question(state).key == "patient_identity"
        await turn(client, 5, "Да")
        assert emulator.write_count == emulator.patch_count == 0
