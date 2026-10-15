"""ФИО и дата отдельными сообщениями проходят тот же проверяемый диалог."""

import pytest
from fixture_bridge import pipeline, turn


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def identity_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@pytest.mark.parametrize(
    "opening",
    [
        "Хочу записаться к терапевту",
        "На когда я записана?",
        "Отмените мою запись",
        "Перенесите мою запись",
    ],
)
async def test_name_then_birth_date(opening, identity_engine):
    async with pipeline(pg_engine=identity_engine) as (client, service, emulator, *_):
        await turn(client, 1, opening)
        state = await service.store.inspect("scenario")
        if state.phase == "verifying_owner":
            await turn(client, 2, "Да")
            offset = 1
        else:
            offset = 0
        named = await turn(client, 2 + offset, "Смирнова Ольга Викторовна")
        assert named["code"] == "BIRTH_DATE_REQUIRED"
        assert named["expect"] == "birth_date"
        state = await service.store.inspect("scenario")
        assert state.identity_draft and "Смирнова" not in state.model_dump_json()
        public = (await client.get("/api/dialog/scenario")).json()
        assert "identity_draft" not in public
        result = await turn(client, 3 + offset, "12 марта 1978 года")
        assert result["code"] not in {
            "NOT_UNDERSTOOD",
            "IDENTIFICATION_REQUIRED",
            "PROCESSING_INTERRUPTED",
        }
        state = await service.store.inspect("scenario")
        assert state.client_id == 1 and state.identity_draft is None
        assert emulator.write_count == 0


async def test_wrong_patronymic_is_not_accepted_and_end_erases_draft():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Хочу записаться к терапевту")
        await turn(client, 2, "Смирнова Ольга Александровна")
        await turn(client, 3, "12 марта 1978 года")
        assert (await service.store.inspect("scenario")).client_id is None
        assert emulator.write_count == 0
        await turn(client, 4, "Смирнова Ольга Викторовна")
        await client.post("/api/dialog/scenario/end")
        assert (await service.store.inspect("scenario")).identity_draft is None
