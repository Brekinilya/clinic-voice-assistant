"""Обновление индекса: кандидат, публикация, переход читателей и честные сбои."""

import asyncio
import os
import stat
import subprocess
import sys
import textwrap
import time as timemod
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.booking.preferences import SlotPreferences
from app.clock import ClinicClock
from app.demo import DemoClock
from app.mis.models import Doctor
from app.mis.ports import BudgetExhausted
from app.scheduling.cli import main, publish_catalog
from app.scheduling.importer import import_workbook
from app.scheduling.index import SqliteSchedule
from app.scheduling.models import Catalog, ScheduleImportError
from app.scheduling.refresh import index_expires_at, read_status, refresh_loop, refresh_once
from app.scheduling.workbook import DOCTOR_HEADER, SLOT_HEADER
from app.settings import Settings

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Обновитель использует Linux flock и атомарную замену открытого SQLite"
)


async def test_stale_catalog_cannot_extend_index_validity(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()
    stale = catalog(clock)
    clock.value += timedelta(minutes=6)
    with pytest.raises(ScheduleImportError, match="CATALOG_NOT_FRESH"):
        await refresh_once(
            settings(),
            clock,
            workbook_path=tmp_path / "live.xlsx",
            index_path=index,
            fetch_catalog_to=await fake_fetch(stale),
        )
    assert index.read_bytes() == before
    assert read_status(index)["error_code"] == "CATALOG_NOT_FRESH"


@pytest.mark.parametrize(
    "arguments", [{"retry_s": 0}, {"margin_s": -1}, {"margin_s": 86400}, {"retry_s": float("nan")}]
)
async def test_refresh_loop_rejects_busy_or_late_intervals(tmp_path, arguments):
    with pytest.raises(ScheduleImportError, match="INVALID_REFRESH_INTERVAL"):
        await refresh_loop(
            settings(),
            DemoClock(),
            workbook_path=tmp_path / "missing.xlsx",
            index_path=tmp_path / "index.sqlite",
            max_cycles=1,
            **arguments,
        )


def test_reader_rejects_file_replaced_during_open(tmp_path, monkeypatch):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    original = SqliteSchedule._file_identity
    calls = 0

    def changed(self):
        nonlocal calls
        calls += 1
        identity = original(self)
        return identity if calls == 1 else (*identity[:3], identity[3] + 1)

    monkeypatch.setattr(SqliteSchedule, "_file_identity", changed)
    with pytest.raises(ScheduleImportError, match="INDEX_CHANGED_DURING_OPEN"):
        SqliteSchedule(index, clock, mode="offline_test")


def workbook(path: Path, *, status="Свободен", start=datetime(2026, 10, 14)):
    book = Workbook()
    info = book.active
    info.title = "Описание"
    info["B4"], info["B6"] = start, start + timedelta(days=17)
    doctors = book.create_sheet("Врачи")
    doctors.append(DOCTOR_HEADER)
    doctors.append(
        ("D001", "Королёва Анна Петровна", "Взрослое", "Хирург", "Взрослые 18+", 203, 30, "График")
    )
    calendar = book.create_sheet("Календарь")
    days = [start + timedelta(days=i) for i in range(18)]
    calendar.append(("ID", "ФИО", "Специальность", *days))
    calendar.append((None, None, None, *["день"] * 18))
    calendar.append(("D001", "Королёва Анна Петровна", "Хирург", *["Р"] * 18))
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
                    "Хирург",
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


def catalog(clock, *, mode="offline_test", doctors=None, **kwargs):
    return Catalog(
        mode=mode,
        base_url="https://offline.invalid/api/v1",
        captured_at=clock.now(),
        doctors=doctors
        or (
            Doctor(id=907, full_name="королева Анна Петровна", specialty="Хирург", is_active=True),
        ),
        **kwargs,
    )


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def publish_live(tmp_path, clock, *, status="Свободен"):
    index = tmp_path / "schedule.sqlite"
    import_workbook(
        workbook(tmp_path / "live.xlsx", status=status), catalog(clock), index, clock.now()
    )
    return index


async def fake_fetch(document):
    async def fetch(destination: Path):
        publish_catalog(destination, document)
        return {"mode": document.mode, "doctors": len(document.doctors)}

    return fetch


async def test_refresh_publishes_validated_index_and_status(tmp_path):
    clock = DemoClock()
    index = tmp_path / "schedule.sqlite"
    status = await refresh_once(
        settings(),
        clock,
        workbook_path=workbook(tmp_path / "source.xlsx"),
        index_path=index,
        fetch_catalog_to=await fake_fetch(catalog(clock)),
    )
    assert status["ok"] is True and status["error_code"] is None
    assert status["generation"] and status["expires_at"] and status["last_success_at"]
    assert read_status(index) == status
    reader = SqliteSchedule(index, clock, mode="offline_test")
    slots = reader.find("Хирург", SlotPreferences(), clock.now())
    assert slots and {slot.doctor_id for slot in slots} == {907}
    reader.close()
    # Staging не остаётся рядом с рабочим поколением.
    assert not list(tmp_path.glob("*.refresh-candidate.sqlite"))
    assert not list(tmp_path.glob("*.refresh-catalog.json"))


async def test_refresh_rejects_wrong_token_scope_and_keeps_live(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()
    live_settings = settings(app_mode="hackathon", mis_token="fictional-team-token")
    foreign = catalog(clock, mode="hackathon", token_scope="b" * 64)
    with pytest.raises(ScheduleImportError, match="INDEX_SCOPE_MISMATCH"):
        await refresh_once(
            live_settings,
            clock,
            workbook_path=workbook(tmp_path / "source.xlsx"),
            index_path=index,
            fetch_catalog_to=await fake_fetch(foreign),
        )
    assert index.read_bytes() == before
    status = read_status(index)
    assert status["ok"] is False and status["error_code"] == "INDEX_SCOPE_MISMATCH"


async def test_refresh_corrupted_workbook_and_ambiguous_doctor_keep_live(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")
    with pytest.raises(ScheduleImportError, match="INVALID_WORKBOOK"):
        await refresh_once(
            settings(),
            clock,
            workbook_path=broken,
            index_path=index,
            fetch_catalog_to=await fake_fetch(catalog(clock)),
        )
    assert index.read_bytes() == before

    twins = catalog(
        clock,
        doctors=(
            Doctor(id=907, full_name="королева Анна Петровна", specialty="Хирург", is_active=True),
            Doctor(id=908, full_name="королева Анна Петровна", specialty="Хирург", is_active=True),
        ),
    )
    with pytest.raises(ScheduleImportError, match="DOCTOR_NOT_UNIQUELY_MATCHED"):
        await refresh_once(
            settings(),
            clock,
            workbook_path=workbook(tmp_path / "source.xlsx"),
            index_path=index,
            fetch_catalog_to=await fake_fetch(twins),
        )
    assert index.read_bytes() == before


async def test_refresh_budget_exhausted_keeps_live_and_records_code(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()

    async def exhausted(destination: Path):
        raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=30)

    with pytest.raises(BudgetExhausted, match="LOCAL_RATE_LIMIT"):
        await refresh_once(
            settings(),
            clock,
            workbook_path=workbook(tmp_path / "source.xlsx"),
            index_path=index,
            fetch_catalog_to=exhausted,
        )
    assert index.read_bytes() == before
    assert read_status(index)["error_code"] == "LOCAL_RATE_LIMIT"


async def test_two_refreshers_only_one_publishes(tmp_path):
    clock = DemoClock()
    index = tmp_path / "schedule.sqlite"
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch(destination: Path):
        started.set()
        await release.wait()
        publish_catalog(destination, catalog(clock))

    first = asyncio.create_task(
        refresh_once(
            settings(),
            clock,
            workbook_path=workbook(tmp_path / "source.xlsx"),
            index_path=index,
            fetch_catalog_to=slow_fetch,
        )
    )
    await started.wait()
    with pytest.raises(ScheduleImportError, match="REFRESH_ALREADY_RUNNING"):
        await refresh_once(
            settings(),
            clock,
            workbook_path=tmp_path / "source.xlsx",
            index_path=index,
            fetch_catalog_to=await fake_fetch(catalog(clock)),
        )
    release.set()
    status = await first
    assert status["ok"] is True
    # Второй обновитель не перетёр статус первого своей неудачей.
    assert read_status(index)["finished_at"] == status["finished_at"]


async def test_crash_leftovers_are_not_treated_as_live(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()
    (tmp_path / "schedule.sqlite.refresh-candidate.sqlite").write_bytes(b"garbage")
    (tmp_path / "schedule.sqlite.refresh-catalog.json").write_bytes(b"garbage")

    async def failing(destination: Path):
        raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=5)

    with pytest.raises(BudgetExhausted):
        await refresh_once(
            settings(),
            clock,
            workbook_path=tmp_path / "live.xlsx",
            index_path=index,
            fetch_catalog_to=failing,
        )
    assert index.read_bytes() == before
    assert not list(tmp_path.glob("*.refresh-candidate.sqlite"))
    status = await refresh_once(
        settings(),
        clock,
        workbook_path=tmp_path / "live.xlsx",
        index_path=index,
        fetch_catalog_to=await fake_fetch(catalog(clock)),
    )
    assert status["ok"] is True


async def test_open_reader_moves_to_new_generation(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    reader = SqliteSchedule(index, clock, mode="offline_test")
    assert reader.find("Хирург", SlotPreferences(), clock.now())
    first_generation = reader.imported_at

    clock.value += timedelta(hours=1)
    await refresh_once(
        settings(),
        clock,
        workbook_path=workbook(tmp_path / "booked.xlsx", status="Записан"),
        index_path=index,
        fetch_catalog_to=await fake_fetch(catalog(clock)),
    )
    # До интервала проверки допустим прежний снимок; после — уже новое поколение.
    clock.value += timedelta(seconds=31)
    assert reader.find("Хирург", SlotPreferences(), clock.now()) == ()
    assert reader.imported_at > first_generation
    # Соединение прежнего снимка закрыто отложенно: начатый на нём поиск не обрывается.
    retired = reader._retired[0]
    assert retired.execute("SELECT count(*) FROM slots").fetchone()[0] > 0
    reader.close()


def test_reader_keeps_working_snapshot_when_candidate_is_invalid(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    reader = SqliteSchedule(index, clock, mode="offline_test")
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"not sqlite at all" * 100)
    os.replace(garbage, index)
    clock.value += timedelta(seconds=31)
    assert reader.find("Хирург", SlotPreferences(), clock.now())
    assert reader.reload_error == "INDEX_RELOAD_FAILED"
    reader.close()


async def test_expired_index_and_failed_refresh_stay_honest(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    before = index.read_bytes()
    reader = SqliteSchedule(index, clock, mode="offline_test")
    clock.value += timedelta(hours=25)

    async def failing(destination: Path):
        raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=5)

    with pytest.raises(BudgetExhausted):
        await refresh_once(
            settings(),
            clock,
            workbook_path=tmp_path / "live.xlsx",
            index_path=index,
            fetch_catalog_to=failing,
        )
    # Срок не продлён фиктивным timestamp: файл тот же, поиск честно недоступен.
    assert index.read_bytes() == before
    with pytest.raises(ScheduleImportError, match="SCHEDULE_NOT_CURRENT"):
        reader.find("Хирург", SlotPreferences(), clock.now())
    with pytest.raises(ScheduleImportError, match="SCHEDULE_NOT_CURRENT"):
        SqliteSchedule(index, clock, mode="offline_test")
    reader.close()
    assert index_expires_at(index) is not None and index_expires_at(index) < clock.now()


async def test_loop_skips_fetch_until_due_then_refreshes_once(tmp_path):
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    calls = 0

    async def counting_fetch(destination: Path):
        nonlocal calls
        calls += 1
        publish_catalog(destination, catalog(clock))

    async def fake_sleep(seconds: float):
        clock.value += timedelta(seconds=seconds)

    source = tmp_path / "live.xlsx"
    await refresh_loop(
        settings(),
        clock,
        workbook_path=source,
        index_path=index,
        fetch_catalog_to=counting_fetch,
        sleep=fake_sleep,
        max_cycles=3,
    )
    assert calls == 0  # свежий индекс: повторный запуск не создаёт поток GET

    clock.value = index_expires_at(index) - timedelta(hours=1)
    await refresh_loop(
        settings(),
        clock,
        workbook_path=source,
        index_path=index,
        fetch_catalog_to=counting_fetch,
        sleep=fake_sleep,
        max_cycles=3,
    )
    assert calls == 1
    assert read_status(index)["ok"] is True


def test_cli_refresh_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # CLI работает по настоящим клиник-часам: период книги строим вокруг сегодня.
    clock = ClinicClock()
    today = clock.now().date()
    start = datetime(today.year, today.month, today.day) - timedelta(days=3)
    catalog_path = tmp_path / "doctors.json"
    publish_catalog(catalog_path, catalog(clock))
    index = tmp_path / "schedule.sqlite"
    assert (
        main(
            [
                "refresh",
                "--workbook",
                str(workbook(tmp_path / "source.xlsx", start=start)),
                "--index",
                str(index),
                "--catalog",
                str(catalog_path),
            ]
        )
        == 0
    )
    assert main(["refresh-status", "--index", str(index)]) == 0
    assert '"ok": true' in capsys.readouterr().out
    reader = SqliteSchedule(index, clock, mode="offline_test")
    assert reader.find("Хирург", SlotPreferences(), clock.now())
    reader.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix-права проверяются на Linux")
def test_published_index_is_readable_by_service_uid(tmp_path):
    clock = DemoClock()
    index = tmp_path / "schedule.sqlite"
    import_workbook(workbook(tmp_path / "source.xlsx"), catalog(clock), index, clock.now())
    mode = stat.S_IMODE(index.stat().st_mode)
    # Сервисный UID10001 — «остальные» для файла владельца импорта: нужно o+r.
    assert mode & 0o004
    assert mode == 0o644


def test_two_process_readers_move_to_new_generation(tmp_path):
    """Настоящие отдельные процессы-читатели, а не два указателя на один объект."""
    clock = DemoClock()
    index = publish_live(tmp_path, clock)
    child = tmp_path / "reader_child.py"
    child.write_text(
        textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from app.booking.preferences import SlotPreferences
            from app.demo import DemoClock
            from app.scheduling.index import SqliteSchedule

            root = Path(sys.argv[1])
            clock = DemoClock()
            reader = SqliteSchedule(root / "schedule.sqlite", clock,
                                    mode="offline_test", reload_interval_s=0)
            first = len(reader.find("Хирург", SlotPreferences(), clock.now()))
            (root / sys.argv[2]).write_text(str(first))
            deadline = time.monotonic() + 30
            while not (root / "go").exists():
                if time.monotonic() > deadline:
                    raise SystemExit("go timeout")
                time.sleep(0.02)
            second = len(reader.find("Хирург", SlotPreferences(), clock.now()))
            (root / sys.argv[3]).write_text(str(second))
            reader.close()
            """
        ),
        encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[1]
    processes = []
    for number in (1, 2):
        processes.append(
            subprocess.Popen(
                [sys.executable, str(child), str(tmp_path), f"first{number}", f"second{number}"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(repo)},
            )
        )
    try:
        for number in (1, 2):
            ready = tmp_path / f"first{number}"
            deadline = timemod.monotonic() + 30
            while not ready.exists():
                assert timemod.monotonic() < deadline, "читатель не стартовал"
                timemod.sleep(0.02)
            assert int(ready.read_text()) > 0
        asyncio.run(
            refresh_once(
                settings(),
                clock,
                workbook_path=workbook(tmp_path / "booked.xlsx", status="Записан"),
                index_path=index,
                fetch_catalog_to=asyncio.run(fake_fetch(catalog(clock))),
            )
        )
        (tmp_path / "go").write_text("1")
        for process in processes:
            assert process.wait(timeout=30) == 0
        for number in (1, 2):
            assert (tmp_path / f"second{number}").read_text() == "0"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
