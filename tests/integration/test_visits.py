"""Справка через HTTP: принадлежность, будущие записи, свежесть и сохранённые страницы."""

import asyncio
from datetime import timedelta

import pytest
from fixture_bridge import pipeline, turn

from app.mis.budget import MemoryBudget
from app.mis.models import VisitStatus, parse_date
from app.mis.ports import BudgetExhausted, MisUnavailable
from app.visits.models import VisitQuery

IDENTITY = "Кузьмина Нина Андреевна, двадцатое июля пятьдесят пятого"
REQUEST = "Подскажите, на какое время я записана к окулисту?"


def records(*times):
    return {"visits": [{"client": "kuzmina_na", "doctor": "lebedev", "at": at} for at in times]}


@pytest.mark.parametrize("specialty", ["ОТОРИНОЛАРИНГОЛОГ", "отоларинголог", "лор"])
async def test_visit_filter_normalizes_ent_alias_but_keeps_child_profile_separate(specialty):
    setup = {"visits": [{"client": "ivanov_sp", "doctor": "belova", "at": "05.10.2026 13:20"}]}
    async with pipeline(setup) as (_, _, emulator, *_):
        visit = emulator.records["demo"][1]
        visit = visit.model_copy(
            update={"doctor": visit.doctor.model_copy(update={"specialty": specialty})}
        )
        assert VisitQuery(specialty="лор").matches(visit)
        assert not VisitQuery(specialty="детский лор").matches(visit)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def visit_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


async def test_read_only_query_replay_and_cleanup(visit_engine):
    async with pipeline(records("05.10.2026 13:20"), pg_engine=visit_engine) as (
        client,
        service,
        emulator,
        *_,
    ):
        initial = dict(emulator.records["demo"])
        first = await turn(client, 1, REQUEST)
        assert first["code"] == "IDENTIFICATION_REQUIRED" and first["visits"] == []
        result = await turn(client, 2, IDENTITY)
        assert result["code"] == "INFO_GIVEN" and result["visits"][0]["room"] == "118"
        assert await turn(client, 2, IDENTITY) == result
        state = await service.store.inspect("scenario")
        assert state.visit_items[0].visit_id == 1 and state.query_for_self
        assert state.visit_query.specialty == "офтальмолог"
        final = await turn(client, 3, "Нет, спасибо")
        assert final["phase"] == "ended" and final["visits"] == []
        assert (await service.store.inspect("scenario")).visit_items == ()
        assert emulator.records["demo"] == initial and emulator.write_count == 0


@pytest.mark.parametrize(
    "phrase",
    [
        "Мои записи",
        "Посмотреть мои записи",
        "Хочу свою запись посмотреть",
        "Хочу найти свою запись",
        "Хочу проверить свои приёмы",
    ],
)
async def test_plain_language_visit_request_starts_owned_visit_lookup(phrase):
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, phrase)
        assert result["code"] == "IDENTIFICATION_REQUIRED", result
        assert result["phase"] == "identifying"
        state = await service.store.inspect("scenario")
        assert state.purpose == "visits" and state.query_for_self
        assert state.client_id is None
        assert all(entry[1:3] == ("GET", "doctors") for entry in emulator.log)
        assert emulator.write_count == emulator.patch_count == 0


async def test_stable_cursor_pages_after_record_changes(visit_engine):
    setup = records(*[f"05.10.2026 {hour:02}:20" for hour in range(9, 14)])
    async with pipeline(setup, pg_engine=visit_engine) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, IDENTITY)
        assert [v["visit_id"] for v in result["visits"]] == [1, 2, 3]
        assert result["has_more_visits"]
        # Удаление раннего элемента не должно сдвинуть offset и пропустить четвёртую запись.
        emulator.records["demo"][1] = emulator.records["demo"][1].model_copy(
            update={"status": VisitStatus.CANCELLED}
        )
        result = await turn(client, 3, "Да")
        assert [v["visit_id"] for v in result["visits"]] == [4, 5]
        assert not result["has_more_visits"] and emulator.write_count == 0
        state = await service.store.inspect("scenario")
        assert state.visit_cursor.visit_id == 5


async def test_existing_visits_are_not_limited_by_booking_horizon():
    setup = records("29.09.2026 13:20", "30.09.2026 10:15", "30.09.2026 10:16", "30.11.2026 13:20")
    setup["visits"] += [
        {
            "client": "kuzmina_na",
            "doctor": "lebedev",
            "at": "06.10.2026 13:20",
            "status": "Отменена",
        },
        {
            "client": "kuzmina_na",
            "doctor": "lebedev",
            "at": "07.10.2026 13:20",
            "status": "Завершена",
        },
        {"client": "ivanov_sp", "doctor": "belova", "at": "08.10.2026 13:20"},
    ]
    async with pipeline(setup) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, IDENTITY)
        assert [v["visit_id"] for v in result["visits"]] == [3, 4]
        assert emulator.write_count == 0


async def test_specialty_refinement_keeps_query_instead_of_starting_booking():
    setup = records("05.10.2026 13:20")
    setup["visits"].append(
        {"client": "kuzmina_na", "doctor": "kuznetsova", "at": "06.10.2026 13:20"}
    )
    async with pipeline(setup) as (client, service, emulator, *_):
        await turn(client, 1, "На когда я записана?")
        assert len((await turn(client, 2, IDENTITY))["visits"]) == 2
        result = await turn(client, 3, "А у терапевта?")
        assert result["code"] == "INFO_GIVEN"
        assert [v["specialty"] for v in result["visits"]] == ["терапевт"]
        assert (await service.store.inspect("scenario")).purpose == "visits"
        result = await turn(client, 4, "Запишите к лору")
        assert (await service.store.inspect("scenario")).purpose == "booking"
        assert result["visits"] == [] and emulator.write_count == 0


async def test_date_refinement_discards_previous_page_cursor():
    setup = records("01.10.2026 13:20", "02.10.2026 13:20", "05.10.2026 13:20", "06.10.2026 13:20")
    async with pipeline(setup) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        assert (await turn(client, 2, IDENTITY))["has_more_visits"]
        result = await turn(client, 3, "В пятницу")
        assert result["code"] == "INFO_GIVEN"
        assert [v["visit_id"] for v in result["visits"]] == [2]
        assert not result["has_more_visits"] and emulator.write_count == 0


async def test_no_visits_offers_new_booking_without_creating_it():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        assert (await turn(client, 2, IDENTITY))["code"] == "NO_ACTIVE_VISITS"
        result = await turn(client, 3, "Да")
        assert result["phase"] == "identified" and result["visits"] == []
        assert (await service.store.inspect("scenario")).purpose == "booking"
        assert emulator.write_count == 0


async def test_refresh_reads_actual_status_and_missing_room_is_explicit():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        service.conversation.visits.visits.rooms = None
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, IDENTITY)
        assert result["visits"][0]["room"] is None
        assert "отсутствует" in " ".join(result["utterances"])
        assert "118" not in " ".join(result["utterances"])
        emulator.records["demo"][1] = emulator.records["demo"][1].model_copy(
            update={"status": VisitStatus.CANCELLED}
        )
        result = await turn(client, 3, "Повторите, пожалуйста")
        assert result["code"] == "NO_ACTIVE_VISITS" and result["visits"] == []


async def test_foreign_record_in_mis_response_is_not_disclosed():
    setup = {"visits": [{"client": "ivanov_sp", "doctor": "belova", "at": "05.10.2026 13:20"}]}
    async with pipeline(setup) as (client, service, emulator, *_):

        async def incorrect_filter(**kwargs):
            return list(emulator.records["demo"].values())

        service.booking.mis.visits = incorrect_filter
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, IDENTITY)
        assert result["code"] == "MIS_UNAVAILABLE" and result["visits"] == []
        assert "Белова" not in " ".join(result["utterances"])
        assert emulator.write_count == 0


async def test_failure_before_visit_query_does_not_echo_saved_page():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        await turn(client, 2, IDENTITY)

        async def limited(*args, **kwargs):
            raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=60)

        # Справочник для понимания локальный, поэтому отказ — на первом чтении справки.
        service.booking.mis.client = limited
        result = await turn(client, 3, "Повторите")
        assert result["code"] == "WAIT_MIS" and result["visits"] == []
        assert "Лебедев" not in " ".join(result["utterances"])


async def test_failed_refresh_does_not_echo_saved_visits():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)
        assert (await turn(client, 2, IDENTITY))["code"] == "INFO_GIVEN"

        async def unavailable(**kwargs):
            raise MisUnavailable("TRANSPORT_ERROR")

        service.booking.mis.visits = unavailable
        result = await turn(client, 3, "Повторите")
        assert result["code"] == "MIS_UNAVAILABLE" and result["visits"] == []
        assert "Лебедев" not in " ".join(result["utterances"])


@pytest.mark.parametrize(
    "text", ["На когда записана моя мама?", "Я узнаю запись моей жены", "Когда записана она?"]
)
async def test_third_party_query_does_not_lookup_or_disclose(text):
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):

        async def forbidden(*args, **kwargs):
            raise AssertionError("карточки и посещения постороннего не читаются")

        service.booking.mis.clients = forbidden
        service.booking.mis.visits = forbidden
        result = await turn(client, 1, text)
        assert result["phase"] == "handoff" and result["visits"] == []
        assert emulator.write_count == 0


async def test_booking_for_an_adult_relative_does_not_disclose_existing_visits():
    async with pipeline() as (client, service, emulator, *_):
        first = await turn(client, 1, "Запишите маму к хирургу")
        state = await service.store.inspect("scenario")
        assert state.guardian_call and state.purpose == "booking"
        assert first["transfer"] is None and first["code"] != "PRIVACY_TRANSFER"

        second = await turn(client, 2, IDENTITY)
        assert second["code"] == "GUARDIAN_RELATION_REQUIRED"
        assert "найден" not in " ".join(second["utterances"]).casefold()

        result = await turn(client, 3, "Мама")
        assert result["transfer"] is None, result
        assert result["phase"] in {"offered", "identified", "waitlist_consent"}
        assert not result["visits"]
        assert "Кузьмина" not in " ".join(result["utterances"])
        assert emulator.write_count == emulator.patch_count == 0


async def test_unknown_owner_and_sms_yes_do_not_authorize_disclosure():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        result = await turn(client, 1, "На когда записана Кузьмина Нина?")
        assert result["code"] == "OWNER_CONFIRMATION_REQUIRED"
        result = await turn(client, 2, "Да, отправьте")
        assert result["code"] == "SMS_UNAVAILABLE" and result["visits"] == []
        assert not (await service.store.inspect("scenario")).query_for_self
        assert emulator.write_count == 0


async def test_confirmed_self_query_still_requires_identity():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        await turn(client, 1, "Какой кабинет?")
        result = await turn(client, 2, "Да")
        assert result["code"] == "IDENTIFICATION_REQUIRED" and result["visits"] == []
        assert (await turn(client, 3, IDENTITY))["code"] == "INFO_GIVEN"


async def test_ambiguous_birth_date_never_queries_patient_cards():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, *_):
        await turn(client, 1, REQUEST)

        async def forbidden(*args):
            raise AssertionError("нельзя выбрать один из двух веков рождения")

        service.booking.mis.clients = forbidden
        result = await turn(client, 2, "Кузьмина Нина Андреевна, двадцатое июля двадцатого")
        assert result["code"] == "BIRTH_DATE_AMBIGUOUS" and result["visits"] == []


async def test_same_name_different_birth_date_never_selects_first_card():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, clock, _, ids):
        original = emulator.clients["demo"][ids["kuzmina_na"]]
        emulator.clients["demo"][99] = original.model_copy(
            update={"id": 99, "birth_date": parse_date("20.07.1995")}
        )
        emulator.records["demo"][2] = emulator.records["demo"][1].model_copy(
            update={"id": 2, "client_id": 99}
        )
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, "Кузьмина Нина Андреевна, 20.07.1995")
        assert [v["visit_id"] for v in result["visits"]] == [2]
        assert (await service.store.inspect("scenario")).client_id == 99


async def test_duplicate_identity_requires_retry_then_operator():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, clock, _, ids):
        original = emulator.clients["demo"][ids["kuzmina_na"]]
        emulator.clients["demo"][99] = original.model_copy(update={"id": 99})
        await turn(client, 1, REQUEST)
        assert (await turn(client, 2, IDENTITY))["code"] == "IDENTIFICATION_REQUIRED"
        result = await turn(client, 3, IDENTITY)
        assert result["code"] == "IDENTIFICATION_POLICY_REQUIRED" and result["visits"] == []
        result = await turn(client, 4, "1234567890123456")
        assert result["transfer"]["route"] == "R-07" and result["visits"] == []


async def test_local_budget_wait_keeps_identified_client_and_query():
    async with pipeline(records("05.10.2026 13:20")) as (client, service, emulator, clock, *_):
        service.booking.mis._budget = MemoryBudget(clock, limit=2)
        await turn(client, 1, REQUEST)
        result = await turn(client, 2, IDENTITY)
        assert result["code"] == "WAIT_MIS" and result["visits"] == []
        current = await service.store.inspect("scenario")
        assert current.client_id is not None and current.visit_query.specialty == "офтальмолог"
        assert await turn(client, 2, IDENTITY) == result
        clock.value += timedelta(seconds=60)
        assert (await turn(client, 3, REQUEST))["code"] == "INFO_GIVEN"
        assert emulator.write_count == 0


async def test_medical_handoff_clears_previously_shown_information(visit_engine):
    async with pipeline(records("05.10.2026 13:20"), pg_engine=visit_engine) as (
        client,
        service,
        emulator,
        *_,
    ):
        await turn(client, 1, REQUEST)
        await turn(client, 2, IDENTITY)
        result = await turn(client, 3, "Какую таблетку принять?")
        assert result["code"] == "MEDICAL_BOUNDARY" and result["visits"] == []
        assert result["transfer"] is None
        assert (await service.store.inspect("scenario")).visit_items == ()
        result = await turn(client, 4, "Нет, скажите, что мне выпить")
        assert result["transfer"]["route"] == "R-04" and result["visits"] == []


async def test_medical_partial_during_query_prevents_late_disclosure(visit_engine):
    async with pipeline(records("05.10.2026 13:20"), pg_engine=visit_engine) as (
        client,
        service,
        emulator,
        *_,
    ):
        await turn(client, 1, REQUEST)
        started, finish = asyncio.Event(), asyncio.Event()
        original = service.booking.mis.visits

        async def delayed(**kwargs):
            started.set()
            await finish.wait()
            return await original(**kwargs)

        service.booking.mis.visits = delayed
        task = asyncio.create_task(turn(client, 2, IDENTITY))
        await started.wait()
        result = await turn(client, 3, "Какую таблетку выпить?", final=False)
        assert result["kind"] == "partial" and result["transfer"] is None
        assert result["visits"] == []
        finish.set()
        late = await task
        assert late["code"] == "TURN_INTERRUPTED" and late["visits"] == []
        assert "Лебедев" not in " ".join(late["utterances"])
        assert (await turn(client, 2, IDENTITY))["visits"] == []
        boundary = await turn(client, 3, "Какую таблетку выпить?")
        assert boundary["code"] == "MEDICAL_BOUNDARY" and boundary["visits"] == []
