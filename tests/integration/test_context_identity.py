"""Обычный текст нового ФИО не меняет дату приёма и не сохраняет прежнее согласие."""

from datetime import date

import pytest
from fixture_bridge import pipeline, turn
from test_budget_dialogue import reserve
from test_cancellation import SETUP as CANCEL_SETUP
from test_cancellation import prepare as prepare_cancel
from test_rescheduling import SETUP as MOVE_SETUP
from test_rescheduling import prepare as prepare_move

from app.dialogue.models import Turn


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def identity_context_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@pytest.mark.parametrize(
    "text",
    [
        "Сидоров Павел Иванович, 01.01.1980",
        "Меня зовут Сидоров Павел Иванович, первое января восьмидесятого",
    ],
)
async def test_unknown_identity_at_confirmation_cannot_book_old_patient(
    identity_context_engine, text
):
    async with pipeline(pg_engine=identity_context_engine) as (client, service, emulator, *_):
        await reserve(client)
        before = await service.store.inspect("scenario")
        result = await turn(client, 4, text)
        assert result["code"] == "IDENTIFICATION_REQUIRED"
        after = await service.store.inspect("scenario")
        assert after.client_id is None and after.proposal is None and not after.options
        assert after.preferences == before.preferences
        await turn(client, 5, "Да")
        assert emulator.write_count == emulator.patch_count == 0


async def test_full_identity_without_birth_date_replaces_the_context(identity_context_engine):
    async with pipeline(pg_engine=identity_context_engine) as (client, service, emulator, *_):
        await reserve(client)
        result = await turn(client, 4, "Петров Игорь Николаевич")
        assert result["code"] == "BIRTH_DATE_REQUIRED" and result["expect"] == "birth_date"
        after = await service.store.inspect("scenario")
        assert after.client_id is None and after.proposal is None and after.identity_draft
        assert "Петров" not in after.model_dump_json()
        await turn(client, 5, "Пятое мая тысяча девятьсот шестьдесят второго")
        state = await service.store.inspect("scenario")
        assert state.client_id == 3
        assert state.preferences.date_from == date(2026, 10, 2)
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("mode", ["cancel", "move"])
async def test_new_text_identity_revokes_existing_visit_action(identity_context_engine, mode):
    setup, prepare, next_turn = (
        (CANCEL_SETUP, prepare_cancel, 4) if mode == "cancel" else (MOVE_SETUP, prepare_move, 5)
    )
    async with pipeline(setup, pg_engine=identity_context_engine) as (
        client,
        service,
        emulator,
        *_,
    ):
        await prepare(client)
        result = await turn(client, next_turn, "Сидоров Павел Иванович, 01.01.1980")
        assert result["code"] == "IDENTIFICATION_REQUIRED"
        state = await service.store.inspect("scenario")
        assert state.client_id is None
        assert state.proposal is state.cancel_proposal is state.move_proposal is None
        await turn(client, next_turn + 1, "Да")
        assert emulator.write_count == emulator.patch_count == 0


async def test_doctor_full_name_is_not_treated_as_patient_identity():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, "Смирнова Ольга Викторовна, 12.03.1978")
        state = await service.store.inspect("scenario")
        result = await service.conversation.input.parse(
            state, Turn(turn_id=3, text="Кузнецова Анна Сергеевна")
        )
        assert result.slots.last_name is None and result.slots.birth_date is None
        assert emulator.write_count == 0
