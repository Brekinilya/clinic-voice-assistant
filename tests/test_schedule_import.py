"""Импорт, поиск и запись проверяются на учебной книге с непоследовательными ID МИС."""

import hashlib
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook, load_workbook

from app.backend import open_backend
from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError, Slot
from app.booking.preferences import SlotPreferences
from app.booking.schedule import AccessFact, AccessState
from app.booking.service import BookingService
from app.cancellation.models import summary
from app.clock import CLINIC_TIMEZONE
from app.demo import DemoClock, EmulatorBudget, demo_emulator
from app.dialogue.bootstrap import open_command_backend
from app.dialogue.models import Turn
from app.mis.http import HttpMedicalSystem
from app.mis.models import Client, Doctor
from app.rescheduling.memory import MemoryMoveStore
from app.rescheduling.service import RescheduleService
from app.scheduling.access import access_state
from app.scheduling.bootstrap import configured_schedule, read_document, token_scope
from app.scheduling.cli import capture_catalog, main, publish_catalog
from app.scheduling.importer import digest, import_workbook
from app.scheduling.index import SqliteSchedule
from app.scheduling.matching import match_doctors
from app.scheduling.models import Catalog, ScheduleImportError, WorkbookDoctor
from app.scheduling.workbook import DOCTOR_HEADER, SLOT_HEADER
from app.settings import Settings


def workbook(path: Path, *, specialty="Хирург", group="Взрослые 18+", status="Свободен"):
    book = Workbook()
    info = book.active
    info.title = "Описание"
    info["B4"], info["B6"] = datetime(2026, 10, 14), datetime(2026, 10, 31)
    doctors = book.create_sheet("Врачи")
    doctors.append(DOCTOR_HEADER)
    doctors.append(
        ("D001", "Королёва Анна Петровна", "Взрослое", specialty, group, 203, 30, "График")
    )
    calendar = book.create_sheet("Календарь")
    days = [datetime(2026, 10, 14) + timedelta(days=i) for i in range(18)]
    calendar.append(("ID", "ФИО", "Специальность", *days))
    calendar.append((None, None, None, *["день"] * 18))
    calendar.append(("D001", "Королёва Анна Петровна", specialty, *["Р"] * 18))
    book.create_sheet("Недоступность").append(
        ("ID врача", "ФИО врача", "Специальность", "Тип", "С", "По")
    )
    slots = book.create_sheet("Слоты")
    slots.append(SLOT_HEADER)
    for day in days:
        for hour in (10, 11, 12):
            slots.append(
                (
                    f"S-{day.day}-{hour}",
                    day,
                    "день",
                    "D001",
                    "Королёва Анна Петровна",
                    specialty,
                    "Взрослое",
                    203,
                    time(hour),
                    time(hour, 30),
                    30,
                    status,
                )
            )
    book.save(path)
    book.close()
    return path


def catalog(clock, *, specialty="Хирург", active=True, mode="offline_test", **kwargs):
    return Catalog(
        mode=mode,
        base_url="https://offline.invalid/api/v1",
        captured_at=clock.now(),
        doctors=(
            Doctor(
                id=907, full_name="королева Анна Петровна", specialty=specialty, is_active=active
            ),
        ),
        **kwargs,
    )


@pytest.fixture
def indexed(tmp_path):
    clock = DemoClock()
    source = workbook(tmp_path / "source.xlsx")
    document = catalog(clock)
    path = tmp_path / "schedule.sqlite"
    report = import_workbook(source, document, path, clock.now())
    schedule = SqliteSchedule(path, clock, mode="offline_test")
    yield clock, source, path, document, report, schedule
    schedule.close()


def patient(birth=date(1990, 1, 1)):
    return Client(
        id=1,
        last_name="Тестов",
        first_name="Александр",
        middle_name="Примерович",
        birth_date=birth,
        comment=None,
        created_at=DemoClock().now(),
    )


def test_full_import_uses_mis_ids_and_preserves_source(indexed):
    clock, source, path, document, report, schedule = indexed
    assert report.doctors == 1 and report.slots == report.free_slots == 54
    assert report.workbook_sha256 == digest(source)
    assert report.catalog_sha256 == hashlib.sha256(document.model_dump_json().encode()).hexdigest()
    slots = schedule.find("хирург", SlotPreferences(), clock.now())
    assert len(slots) == 42 and {s.doctor_id for s in slots} == {907}
    assert all(s.scheduled_at.tzinfo == CLINIC_TIMEZONE for s in slots)
    assert schedule.room(907, slots[0].scheduled_at) == "203"
    assert not schedule.eligible(1, slots[0])
    schedule.remember_patient(patient())
    assert schedule.eligible(1, slots[0])
    assert schedule.room(1, slots[0].scheduled_at) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT excel_id,id FROM doctors").fetchone() == ("D001", 907)
        assert "Тестов" not in " ".join(str(r) for r in db.execute("SELECT * FROM metadata"))


@pytest.mark.parametrize(
    "preferences,expected",
    [
        (SlotPreferences(time_from=time(10), time_to=time(10)), (14, 10)),
        (SlotPreferences(time_from=time(10, 0, 1)), (14, 11)),
        (SlotPreferences(time_to=time(10, 0, 1)), (14, 10)),
        (SlotPreferences(time_pref="late"), (27, 12)),
        (SlotPreferences(date_from=date(2026, 10, 20), date_to=date(2026, 10, 20)), (20, 10)),
        (SlotPreferences(doctor_ids=(1,)), None),
        (SlotPreferences(date_from=date(2026, 10, 28)), None),
        (SlotPreferences(date_to=date(2026, 10, 13)), None),
        (SlotPreferences(exclude_weekdays=(3,)), (15, 10)),
    ],
)
def test_search_bounds_horizon_and_preferences(indexed, preferences, expected):
    clock, *_, schedule = indexed
    result = schedule.find("хирург", preferences, clock.now())
    if expected is None:
        assert not result
    else:
        assert (result[0].scheduled_at.day, result[0].scheduled_at.hour) == expected


def test_minimum_notice_is_inclusive(indexed):
    clock, *_, schedule = indexed
    clock.value = clock.value.replace(hour=9, minute=30)
    assert schedule.find("хирург", SlotPreferences(), clock.now())[0].scheduled_at.hour == 10
    clock.value += timedelta(seconds=1)
    assert schedule.find("хирург", SlotPreferences(), clock.now())[0].scheduled_at.hour == 11


@pytest.mark.parametrize("status", ["Записан", "Приём состоялся", "Неявка", "Отменён пациентом"])
def test_only_source_free_slots_are_offered(tmp_path, status):
    clock = DemoClock()
    path = tmp_path / "schedule.sqlite"
    report = import_workbook(
        workbook(tmp_path / "source.xlsx", status=status), catalog(clock), path, clock.now()
    )
    schedule = SqliteSchedule(path, clock, mode="offline_test")
    try:
        assert report.free_slots == 0 and not schedule.slots()
        assert schedule.room(907, clock.now().replace(hour=10)) == "203"
        assert not schedule.contains(
            Slot(doctor_id=907, specialty="Хирург", scheduled_at=clock.now().replace(hour=10))
        )
    finally:
        schedule.close()


def test_inactive_doctor_is_imported_but_not_offered(tmp_path):
    clock = DemoClock()
    path = tmp_path / "index.sqlite"
    import_workbook(
        workbook(tmp_path / "source.xlsx"), catalog(clock, active=False), path, clock.now()
    )
    schedule = SqliteSchedule(path, clock, mode="offline_test")
    try:
        assert not schedule.find("Хирург", SlotPreferences(), clock.now())
    finally:
        schedule.close()


@pytest.mark.parametrize(
    "change,code",
    [
        ("duplicate", "DUPLICATE_SLOT"),
        ("overlap", "OVERLAPPING_SLOTS"),
        ("duration", "INVALID_SLOT_DURATION"),
        ("room", "SLOT_DOCTOR_MISMATCH"),
        ("status", "INVALID_SLOT_STATUS"),
        ("formula", "INVALID_WORKBOOK_TEXT"),
        ("calendar", "SLOT_DURING_UNAVAILABILITY"),
        ("absence", "SLOT_DURING_UNAVAILABILITY"),
        ("missing_calendar", "INCOMPLETE_CALENDAR"),
        ("missing_sheet", "MISSING_WORKBOOK_SHEET"),
        ("department", "SLOT_DOCTOR_MISMATCH"),
    ],
)
def test_invalid_workbook_does_not_replace_previous_index(tmp_path, change, code):
    clock = DemoClock()
    source, path = workbook(tmp_path / "source.xlsx"), tmp_path / "old.sqlite"
    import_workbook(source, catalog(clock), path, clock.now())
    previous = digest(path)
    book = load_workbook(source)
    slots = book["Слоты"]
    if change == "duplicate":
        slots.append([c.value for c in slots[2]])
    elif change == "overlap":
        slots["I3"], slots["J3"] = time(10, 15), time(10, 45)
    elif change == "duration":
        slots["J2"] = time(10, 45)
    elif change == "room":
        slots["H2"] = 111
    elif change == "status":
        slots["L2"] = "неизвестно"
    elif change == "formula":
        slots["A2"] = "=1+1"
    elif change == "calendar":
        book["Календарь"]["D3"] = "В"
    elif change == "absence":
        book["Недоступность"].append(
            (
                "D001",
                "Королёва Анна Петровна",
                "Хирург",
                "Отпуск",
                datetime(2026, 10, 14),
                datetime(2026, 10, 15),
            )
        )
    elif change == "missing_calendar":
        book["Календарь"]["U1"] = None
    elif change == "department":
        slots["G2"] = "Другое"
    else:
        del book["Описание"]
    book.save(source)
    book.close()
    with pytest.raises(ScheduleImportError, match=code):
        import_workbook(source, catalog(clock), path, clock.now(), replace=True)
    assert digest(path) == previous and not list(tmp_path.glob(".schedule-*"))


def test_publication_refuses_overwrite_and_cleans_temporary_file(indexed):
    clock, source, path, document, *_ = indexed
    with pytest.raises(ScheduleImportError, match="DESTINATION_EXISTS"):
        import_workbook(source, document, path, clock.now())
    assert not list(path.parent.glob(".schedule-*"))


def test_another_import_publishing_first_is_not_overwritten(tmp_path, monkeypatch):
    import os

    clock = DemoClock()
    source, destination = workbook(tmp_path / "source.xlsx"), tmp_path / "schedule.sqlite"
    original_link = os.link

    def concurrent_publish(temporary, target):
        # Другой импорт успел опубликовать файл после нашей начальной проверки.
        target.write_bytes(b"previous-complete-index")
        original_link(temporary, target)

    monkeypatch.setattr("app.scheduling.importer.os.link", concurrent_publish)
    with pytest.raises(ScheduleImportError, match="DESTINATION_EXISTS"):
        import_workbook(source, catalog(clock), destination, clock.now())
    assert destination.read_bytes() == b"previous-complete-index"
    assert not list(tmp_path.glob(".schedule-*"))


def test_index_expiry_and_doctor_identity_are_checked_again(indexed):
    clock, *_, schedule = indexed
    original = schedule.doctors[0]
    assert schedule.matches_doctor(original)
    assert not schedule.matches_doctor(original.model_copy(update={"full_name": "Другой врач"}))
    clock.value += timedelta(days=1)
    with pytest.raises(ScheduleImportError, match="SCHEDULE_NOT_CURRENT"):
        schedule.find("Хирург", SlotPreferences(), clock.now())


def test_mode_and_token_scope_cannot_be_reused(indexed, tmp_path):
    clock, source, path, *_ = indexed
    with pytest.raises(ScheduleImportError, match="INDEX_SCOPE_MISMATCH"):
        SqliteSchedule(
            path,
            clock,
            mode="hackathon",
            base_url="https://offline.invalid/api/v1",
            token_scope="a" * 64,
        )
    live = catalog(clock, mode="hackathon", token_scope="a" * 64)
    live_path = tmp_path / "live.sqlite"
    import_workbook(source, live, live_path, clock.now())
    with pytest.raises(ScheduleImportError, match="INDEX_SCOPE_MISMATCH"):
        SqliteSchedule(
            live_path, clock, mode="hackathon", base_url=live.base_url, token_scope="b" * 64
        )


@pytest.mark.parametrize("kind", ["unknown", "ambiguous", "profile", "name"])
def test_matching_requires_unique_full_name_and_profile(kind):
    source = WorkbookDoctor(
        excel_id="D001",
        full_name="Королёва Анна Петровна",
        specialty="Хирург",
        patient_group="Взрослые 18+",
        room="203",
        minutes=30,
    )
    doctor = catalog(DemoClock()).doctors[0]
    values = () if kind == "unknown" else (doctor,)
    if kind == "ambiguous":
        values += (doctor.model_copy(update={"id": 125}),)
    if kind == "profile":
        values = (doctor.model_copy(update={"specialty": "Детский хирург"}),)
    if kind == "name":
        values = (doctor.model_copy(update={"full_name": "Королева Анна Петрович"}),)
    with pytest.raises(ScheduleImportError):
        match_doctors((source,), values)


def test_ent_alias_does_not_merge_adult_and_child_profiles():
    source = WorkbookDoctor(
        excel_id="D001",
        full_name="Королёва Анна Петровна",
        specialty="ЛОР (отоларинголог)",
        patient_group="Взрослые 18+",
        room="203",
        minutes=30,
    )
    assert (
        match_doctors((source,), catalog(DemoClock(), specialty="Оториноларинголог").doctors)[
            "D001"
        ].id
        == 907
    )
    with pytest.raises(ScheduleImportError):
        match_doctors((source,), catalog(DemoClock(), specialty="Детский отоларинголог").doctors)


@pytest.mark.parametrize(
    "birth,group,profile,expected",
    [
        (date(2008, 10, 15), "Взрослые 18+", "хирург", AccessState.DENIED),
        (date(2008, 10, 14), "Взрослые 18+", "хирург", AccessState.ALLOWED),
        (date(2008, 10, 14), "Дети 0–17 лет", "детский хирург", AccessState.DENIED),
        (date(2025, 10, 15), "Дети до 1 года", "детский хирург", AccessState.ALLOWED),
        (date(2025, 10, 14), "Дети до 1 года", "детский хирург", AccessState.DENIED),
        (None, "Взрослые 18+", "хирург", AccessState.UNKNOWN),
        (date(2027, 1, 1), "Взрослые 18+", "хирург", AccessState.UNKNOWN),
        (date(1990, 1, 1), "Женщины 16+", "Акушер-гинеколог", AccessState.UNKNOWN),
        (date(1990, 1, 1), "Взрослые 18+", "Терапевт", AccessState.UNKNOWN),
        (date(1990, 1, 1), "Взрослые 18+", "Невролог", AccessState.UNKNOWN),
        (date(1990, 1, 1), "Взрослые 18+", "Стоматолог-ортопед", AccessState.UNKNOWN),
    ],
)
def test_age_and_missing_assignment_referral_sex(birth, group, profile, expected):
    assert access_state(birth, 907, profile, group, DemoClock().now(), 1, ()) == expected


@pytest.mark.parametrize(
    "specialty,expected", [("Терапевт", "ELIGIBILITY_UNKNOWN"), ("Хирург", None)]
)
async def test_options_distinguish_missing_access_from_available_slots(
    tmp_path, specialty, expected
):
    """Реальный SQLite-индекс: свободное время не подтверждает участок пациента."""
    from app.mis.emulator import MisEmulator

    clock = DemoClock()
    source = workbook(tmp_path / "access.xlsx", specialty=specialty)
    document = catalog(clock, specialty=specialty)
    path = tmp_path / "access.sqlite"
    import_workbook(source, document, path, clock.now())
    schedule = SqliteSchedule(path, clock, mode="offline_test")
    emulator = MisEmulator(clock, list(document.doctors), {"demo": [patient()]})
    try:
        async with httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=emulator.transport(),
        ) as client:
            service = BookingService(
                HttpMedicalSystem(client, EmulatorBudget()), MemoryBookingStore(), schedule, clock
            )
            if expected:
                with pytest.raises(BookingError, match=expected):
                    await service.options(1, specialty)
            else:
                assert await service.options(1, specialty)
            assert emulator.write_count == 0
    finally:
        schedule.close()


def fact(clock, state=AccessState.ALLOWED):
    return AccessFact(
        client_id=1,
        doctor_id=907,
        state=state,
        valid_from=clock.now(),
        valid_until=clock.now() + timedelta(hours=1),
        source="Учебное подтверждение",
    )


def test_explicit_access_is_time_bound_and_cannot_override_age_or_paid():
    clock = DemoClock()
    values = (fact(clock),)
    birth = date(1990, 1, 1)
    assert (
        access_state(
            birth, 907, "Терапевт", "Взрослые 18+", clock.now() + timedelta(hours=1), 1, values
        )
        == AccessState.ALLOWED
    )
    assert (
        access_state(
            birth,
            907,
            "Терапевт",
            "Взрослые 18+",
            clock.now() + timedelta(hours=1, seconds=1),
            1,
            values,
        )
        == AccessState.UNKNOWN
    )
    assert (
        access_state(date(2020, 1, 1), 907, "Хирург", "Взрослые 18+", clock.now(), 1, values)
        == AccessState.DENIED
    )
    assert (
        access_state(birth, 907, "Стоматолог-ортопед", "Взрослые 18+", clock.now(), 1, values)
        == AccessState.UNKNOWN
    )
    assert (
        access_state(
            birth, 907, "Хирург", "Взрослые 18+", clock.now(), 1, (fact(clock, AccessState.DENIED),)
        )
        == AccessState.DENIED
    )


def test_overlapping_access_facts_rejected(indexed):
    clock, _, path, *_ = indexed
    with pytest.raises(ScheduleImportError, match="ACCESS_OVERLAP"):
        SqliteSchedule(path, clock, mode="offline_test", facts=(fact(clock), fact(clock)))


async def test_booking_uses_index_and_actual_mis_id_without_full_scan(indexed):
    clock, *_, schedule = indexed

    def forbid_scan():
        raise AssertionError("Полный просмотр расписания запрещён")

    schedule.slots = forbid_scan
    emulator = demo_emulator(clock, doctors=schedule.doctors)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        service = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()), MemoryBookingStore(), schedule, clock
        )
        identified = await service.identify("тестов", "АЛЕКСАНДР", "Примерович", date(1990, 1, 1))
        choices = await service.options(identified.id, "хирург", call_id="imported")
        assert len(choices) == 3 and {s.doctor_id for s in choices} == {907}
        action = await service.propose("imported", 1, choices[0])
        result = await service.confirm(
            Approval(
                call_id="imported",
                action_id=action.action_id,
                revision=action.revision,
                turn_id=1,
                is_final=True,
                permitted=True,
            )
        )
        assert result.state == ActionState.BOOKED and emulator.write_count == 1
        assert emulator.records["demo"][result.visit_id].doctor.id == 907
        emulator.doctors[907] = schedule.doctors[0].model_copy(update={"full_name": "Другой врач"})
        with pytest.raises(BookingError, match="DOCTOR_UNAVAILABLE"):
            await service._preflight(1, choices[1], call_id="changed", fresh=True)
        assert emulator.write_count == 1


async def test_bootstrap_supplies_imported_roster_and_rooms(indexed):
    clock, _, path, *_ = indexed
    settings = Settings(
        _env_file=None, backend_demo_enabled=True, dialogue_mode="natural", schedule_index_path=path
    )
    async with open_command_backend(settings, clock) as service:
        assert [d.id for d in await service.booking.mis.doctors()] == [907]
        action = await service.booking.options(1, "хирург", call_id="rooms")
        assert "кабинет двести три" in service.conversation.booked_phrase(
            type("Action", (), {"slot": action[0]})(), "Королёва"
        )


async def test_natural_booking_confirmation_and_result_include_room(indexed):
    clock, _, path, *_ = indexed
    settings = Settings(
        _env_file=None, backend_demo_enabled=True, dialogue_mode="natural", schedule_index_path=path
    )
    async with open_command_backend(settings, clock) as service:
        answers = []
        for number, text in enumerate(
            (
                "Хочу записаться к хирургу",
                "Тестов Александр Примерович, 01.01.1990",
                "Первый",
                "Да",
            ),
            1,
        ):
            answers.append(await service.handle("indexed-natural", Turn(turn_id=number, text=text)))
        assert [r.code for r in answers] == [
            None,
            None,
            "CONFIRMATION_REQUIRED",
            "BOOKED",
        ]
        assert len(answers[1].options) == 2
        assert "кабинет двести три" in " ".join(answers[-2].utterances)
        assert "кабинет двести три" in " ".join(answers[-1].utterances)


async def test_reschedule_uses_index_without_materializing_all_slots(indexed):
    clock, *_, schedule = indexed

    def forbid_scan():
        raise AssertionError("Полный просмотр расписания запрещён")

    schedule.slots = forbid_scan
    emulator = demo_emulator(clock, doctors=schedule.doctors)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, EmulatorBudget())
        from app.mis.models import VisitCreate

        old = await mis.create_visit(
            VisitCreate(
                client_id=1,
                doctor_id=907,
                scheduled_at=clock.now().replace(hour=10) + timedelta(days=1),
            )
        )
        booking = BookingService(mis, MemoryBookingStore(), schedule, clock)
        move = RescheduleService(booking, MemoryMoveStore(booking.store.ledger))
        choices = await move.options(
            1, summary(old), SlotPreferences(date_from=date(2026, 10, 16)), call_id="indexed-move"
        )
        assert len(choices) == 3 and {s.doctor_id for s in choices} == {907}
        prepared = await move.prepare(
            "indexed-move", 1, summary(old), choices[0], schedule.room(907, choices[0].scheduled_at)
        )
        assert prepared.room == "203" and emulator.write_count == 1


async def test_catalog_capture_is_paginated_get_only_and_has_no_token(tmp_path):
    clock = DemoClock()
    settings = Settings(_env_file=None, app_mode="hackathon", mis_token="private-team-token")
    requests = []
    doctors = [
        Doctor(id=1000 + i * 7, full_name=f"Врач {i}", specialty="Хирург", is_active=True)
        for i in range(85)
    ]

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith("/doctors")
        offset, limit = (
            int(request.url.params.get("offset", 0)),
            int(request.url.params.get("limit", 50)),
        )
        return httpx.Response(
            200, json=[d.model_dump(mode="json") for d in doctors[offset : offset + limit]]
        )

    async with httpx.AsyncClient(
        base_url="https://crm-med-mock.dbi-testing.ru/api/v1/",
        transport=httpx.MockTransport(respond),
    ) as client:
        captured = await capture_catalog(
            HttpMedicalSystem(client, EmulatorBudget()), settings, clock
        )
    assert len(captured.doctors) == 85 and len(requests) == 2
    assert (
        captured.token_scope == token_scope(settings)
        and "private-team-token" not in captured.model_dump_json()
    )
    path = tmp_path / "catalog.json"
    publish_catalog(path, captured)
    assert read_document(path, Catalog) == captured
    with pytest.raises(ScheduleImportError, match="DESTINATION_EXISTS"):
        publish_catalog(path, captured)


def test_settings_and_catalog_read_errors_do_not_expose_inputs(tmp_path, capsys, monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, schedule_path="a.json", schedule_index_path="b.sqlite")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, schedule_access_path="a.json")
    document = tmp_path / "duplicate.json"
    document.write_text('{"mode":"offline_test","mode":"hackathon"}', encoding="utf-8")
    with pytest.raises(ScheduleImportError, match="INVALID_DOCUMENT"):
        read_document(document, Catalog)
    monkeypatch.chdir(tmp_path)
    assert main(["check", "secret-missing.sqlite"]) == 1
    assert "secret-missing" not in capsys.readouterr().out


def test_configured_schedule_loads_index(indexed):
    clock, _, path, *_ = indexed
    loaded = configured_schedule(Settings(_env_file=None, schedule_index_path=path), clock)
    try:
        assert loaded.report.slots == 54
    finally:
        loaded.close()


async def test_live_backend_rejects_offline_index_before_any_connections(indexed):
    *_, schedule = indexed
    settings = Settings(_env_file=None, app_mode="hackathon", mis_token="synthetic-token")
    with pytest.raises(ScheduleImportError, match="INDEX_SCOPE_MISMATCH"):
        async with open_backend(settings, schedule):
            pytest.fail("Учебный индекс не должен попасть во внешнюю МИС")


def test_index_keeps_excluded_calendar_dates(indexed):
    clock, *_, schedule = indexed
    excluded = clock.now().date() + timedelta(days=1)
    preferences = SlotPreferences(exclude_dates=(excluded,))
    restored = SlotPreferences.model_validate_json(preferences.model_dump_json())
    assert restored == preferences
    slots = schedule.find("Хирург", restored, clock.now())
    assert slots and all(slot.scheduled_at.date() != excluded for slot in slots)
