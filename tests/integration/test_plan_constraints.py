"""Приёмка T02: настоящий HTTP-диалог, полный справочник и результат в эмуляторе."""

from datetime import date, datetime, timezone

import pytest
from fixture_bridge import pipeline, turn

from app.booking.models import Slot
from app.mis.models import VisitStatus, parse_datetime

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"


@pytest.mark.parametrize(
    "phrase, forbidden",
    [
        ("Запишите к терапевту, только не завтра", {date(2026, 10, 1)}),
        ("Запишите к терапевту в понедельник или в среду", {date(2026, 10, 6)}),
    ],
)
async def test_booking_keeps_date_constraints_until_the_mis_write(phrase, forbidden):
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, phrase)
        offered = await turn(client, 2, IDENTITY)
        assert offered["phase"] == "offered", offered
        state = await service.store.inspect("scenario")
        assert state.options and all(s.scheduled_at.date() not in forbidden for s in state.options)
        held = await turn(client, 3, "Первый")
        assert held["phase"] == "held" and emulator.write_count == 0
        booked = await turn(client, 4, "Да")
        assert booked["code"] == "BOOKED" and emulator.write_count == 1
        assert emulator.records["demo"][booked["visit_id"]].scheduled_at.date() not in forbidden


async def test_explicit_year_outside_the_horizon_does_not_offer_this_year():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на 02.10.2027")
        result = await turn(client, 2, IDENTITY)
        state = await service.store.inspect("scenario")
        assert state.preferences.date_from == date(2027, 10, 2)
        assert result["code"] == "NO_SLOTS" and not state.options
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize(
    "relation",
    ["Мама.", "Мамой.", "Я родитель.", "Родителем."],
)
async def test_child_booking_accepts_parent_relation_inflections(relation):
    async with pipeline() as (client, service, emulator, *_):
        started = await turn(client, 1, "Мне нужно записать дочку к педиатру")
        assert started["phase"] == "identifying", started

        identified = await turn(
            client,
            2,
            "Смирнова Алиса Дмитриевна, седьмое апреля две тысячи девятнадцатого",
        )
        assert identified["code"] == "GUARDIAN_RELATION_REQUIRED"

        offered = await turn(client, 3, relation)
        state = await service.store.inspect("scenario")
        assert offered["phase"] in {"identified", "offered", "waitlist_consent"}, offered
        assert offered["transfer"] is None
        assert state.guardian_call and state.client_id is not None
        assert state.guardian_relation is not None
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("ending", ["", " в десять тридцать"])
async def test_numeric_date_inside_the_horizon_can_be_booked(ending):
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на 02.10.2026" + ending)
        offered = await turn(client, 2, IDENTITY)
        assert offered["phase"] == "offered", offered
        state = await service.store.inspect("scenario")
        assert state.options and all(
            s.scheduled_at.date() == date(2026, 10, 2) for s in state.options
        )
        await turn(client, 3, "Первый")
        booked = await turn(client, 4, "Да")
        assert booked["code"] == "BOOKED" and emulator.write_count == 1
        visit = emulator.records["demo"][booked["visit_id"]]
        assert visit.scheduled_at.date() == date(2026, 10, 2)
        if ending:
            assert (visit.scheduled_at.hour, visit.scheduled_at.minute) == (10, 30)


async def test_negative_date_after_yes_releases_the_previous_booking():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        await turn(client, 3, "Первый")
        old = (await service.store.inspect("scenario")).proposal
        assert old.slot.scheduled_at.date() == date(2026, 10, 1)
        changed = await turn(client, 4, "Да, но только не завтра")
        assert changed["phase"] == "offered" and emulator.write_count == 0
        state = await service.store.inspect("scenario")
        assert state.proposal is None and state.authorization is None
        assert state.options and all(
            s.scheduled_at.date() != date(2026, 10, 1) for s in state.options
        )
        await turn(client, 5, "Да")
        assert emulator.write_count == 0
        await turn(client, 6, "Первый")
        result = await turn(client, 7, "Да")
        assert result["code"] == "BOOKED" and emulator.write_count == 1
        assert emulator.records["demo"][result["visit_id"]].scheduled_at.date() != date(2026, 10, 1)


async def test_booking_selects_surname_from_real_full_name_options():
    async with pipeline() as (client, service, emulator, clock, doctors, clients):
        schedule = service.booking.schedule
        schedule._slots = tuple(
            Slot(doctor_id=doctors[key], specialty="терапевт", scheduled_at=parse_datetime(at))
            for key, at in [("kuznetsova", "01.10.2026 09:00"), ("sergeev", "01.10.2026 10:00")]
        )
        schedule.allowed.add((clients["smirnova_ov"], doctors["sergeev"]))
        await turn(client, 1, "Запишите к терапевту")
        await turn(client, 2, IDENTITY)
        selected = await turn(client, 3, "К Кузнецовой")
        assert selected["phase"] == "held", selected
        result = await turn(client, 4, "Да")
        assert emulator.records["demo"][result["visit_id"]].doctor.id == doctors["kuznetsova"]


@pytest.mark.parametrize("purpose", ["Отмените мою запись", "Перенесите мою запись"])
@pytest.mark.parametrize("selection", ["К Кузнецовой", "К терапевту"])
async def test_cancel_and_move_choose_the_correct_existing_visit(purpose, selection):
    setup = {
        "visits": [
            {"client": "smirnova_ov", "doctor": "kuznetsova", "at": "01.10.2026 08:45"},
            {"client": "smirnova_ov", "doctor": "orlova", "at": "06.10.2026 11:20"},
        ]
    }
    async with pipeline(setup) as (client, service, emulator, *_):
        await turn(client, 1, purpose)
        await turn(client, 2, IDENTITY)
        result = await turn(client, 3, selection)
        state = await service.store.inspect("scenario")
        target = state.cancel_target if state.purpose == "cancel" else state.move_target
        assert target and target.visit_id == 1, result
        assert emulator.write_count == emulator.patch_count == 0
        if state.purpose == "cancel":
            await turn(client, 4, "Нет")  # Отказываемся от предложения переноса.
            clarified = await turn(client, 5, "Отмените запись")
            assert clarified["code"] == "CANCEL_CONFIRMATION_REQUIRED"
            result = await turn(client, 6, "Да")
            assert result["code"] == "CANCELLED", result
            assert emulator.records["demo"][1].status == VisitStatus.CANCELLED
            assert emulator.records["demo"][2].status == VisitStatus.PLANNED
            assert emulator.patch_count == 1


async def test_relative_dates_use_the_clinic_day_at_the_utc_boundary():
    async with pipeline() as (client, service, emulator, clock, *_):
        clock.value = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
        await turn(client, 1, "Запишите к терапевту завтра")
        state = await service.store.inspect("scenario")
        assert state.preferences.date_from == date(2026, 10, 2)
        assert emulator.write_count == 0


@pytest.mark.postgres
async def test_excluded_dates_are_preserved_in_postgres(pg_engine):
    async with pipeline(pg_engine=pg_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту, только не завтра")
        state = await service.store.inspect("scenario")
        assert state.preferences.exclude_dates == (date(2026, 10, 1),)
        offered = await turn(client, 2, IDENTITY)
        assert offered["phase"] == "offered"
        state = await service.store.inspect("scenario")
        assert state.options and all(
            s.scheduled_at.date() != date(2026, 10, 1) for s in state.options
        )
        assert emulator.write_count == 0
