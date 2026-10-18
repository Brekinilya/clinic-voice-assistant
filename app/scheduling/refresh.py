"""Безопасное обновление индекса: кандидат рядом, проверка, атомарная публикация.

Рабочим поколением считается только опубликованный файл индекса: каталог врачей
встроен в его metadata, отдельный файл каталога runtime не читает. Кандидат строится
и проверяется в staging-файлах того же каталога; ошибка до os.replace рабочее
поколение не трогает. Статус содержит только технические поля и безопасные коды.
"""

import asyncio
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from app.mis.ports import MisError
from app.ports import Clock
from app.scheduling.bootstrap import load_index, read_document, token_scope
from app.scheduling.importer import import_workbook
from app.scheduling.models import Catalog, ScheduleImportError
from app.settings import Settings

try:
    import fcntl
except ImportError:  # обновитель рассчитан на Linux-службу; на другой ОС честно отказываем
    fcntl = None

CatalogFetcher = Callable[[Path], Awaitable[object]]


def _lock_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".refresh.lock")


def _status_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".refresh-status.json")


def _staging_catalog(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".refresh-catalog.json")


def _staging_index(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".refresh-candidate.sqlite")


@contextmanager
def _exclusive_lock(index_path: Path):
    """Один обновитель на индекс. flock освобождается ОС при падении процесса."""
    if fcntl is None:
        raise ScheduleImportError("REFRESH_LOCK_UNSUPPORTED")
    descriptor = os.open(_lock_path(index_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScheduleImportError("REFRESH_ALREADY_RUNNING") from None
        yield
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    _fsync_file(path)


def _write_status(index_path: Path, status: dict) -> dict:
    destination = _status_path(index_path)
    descriptor, filename = tempfile.mkstemp(prefix=".refresh-status-", dir=destination.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(status, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        _fsync_dir(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return status


def read_status(index_path: Path) -> dict:
    """Последний записанный статус обновителя; секретов и сырого ответа МИС в нём нет."""
    try:
        with _status_path(index_path).open("rb") as stream:
            data = stream.read(65_537)
        if len(data) > 65_536:
            raise ValueError("status too large")
        status = json.loads(data)
        if not isinstance(status, dict):
            raise ValueError("status is not an object")
        return status
    except (OSError, ValueError):
        return {"ok": None, "error_code": "NO_STATUS"}


def _previous_success(index_path: Path) -> dict:
    previous = read_status(index_path)
    if previous.get("last_success_at"):
        return previous
    return {}


def _record_failure(index_path: Path, clock: Clock, code: str) -> None:
    status = _previous_success(index_path)
    status.update({"ok": False, "finished_at": clock.now().isoformat(), "error_code": code})
    status.setdefault("last_success_at", None)
    _write_status(index_path, status)


def _check_catalog_scope(catalog: Catalog, settings: Settings) -> None:
    # Та же проверка области, что перед ручным import-xlsx: учебные ID не переносятся на стенд.
    if catalog.mode != settings.app_mode.value or (
        catalog.mode == "hackathon"
        and (
            catalog.base_url.rstrip("/") != str(settings.mis_base_url).rstrip("/")
            or catalog.token_scope != token_scope(settings)
        )
    ):
        raise ScheduleImportError("INDEX_SCOPE_MISMATCH")


def index_expires_at(index_path: Path) -> datetime | None:
    """Срок опубликованного поколения из его metadata; повреждённый файл — None."""
    try:
        db = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            record = db.execute("SELECT data FROM metadata WHERE id=1").fetchone()
            expires = datetime.fromisoformat(json.loads(record[0])["expires_at"])
            return expires if expires.tzinfo is not None else None
        finally:
            db.close()
    except Exception:
        return None


async def refresh_once(
    settings: Settings,
    clock: Clock,
    *,
    workbook_path: Path,
    index_path: Path,
    fetch_catalog_to: CatalogFetcher | None = None,
    catalog_path: Path | None = None,
    valid_hours: int = 24,
) -> dict:
    """Один цикл обновления. При ошибке рабочее поколение индекса не заменяется."""
    if catalog_path is None and fetch_catalog_to is None:
        raise ScheduleImportError("CATALOG_SOURCE_REQUIRED")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(index_path):
        staging_catalog = _staging_catalog(index_path)
        staging_index = _staging_index(index_path)
        try:
            if not workbook_path.is_file() or workbook_path.resolve() == index_path.resolve():
                raise ScheduleImportError("WORKBOOK_SOURCE_UNAVAILABLE")
            # Остатки упавшего цикла не считаются ни кандидатом, ни рабочим файлом.
            staging_catalog.unlink(missing_ok=True)
            staging_index.unlink(missing_ok=True)
            if catalog_path is not None:
                catalog = read_document(catalog_path, Catalog)
            else:
                await fetch_catalog_to(staging_catalog)
                catalog = read_document(staging_catalog, Catalog)
            _check_catalog_scope(catalog, settings)
            # Повторная обработка старого JSON не является новым чтением МИС.
            # Разрешён лишь небольшой запас на получение/передачу свежего каталога.
            age = clock.now() - catalog.captured_at
            if age < timedelta(0) or age > timedelta(minutes=5):
                raise ScheduleImportError("CATALOG_NOT_FRESH")
            report = import_workbook(
                workbook_path,
                catalog,
                staging_index,
                clock.now(),
                replace=True,
                valid_hours=valid_hours,
            )
            # Кандидат проходит те же проверки, что индекс при старте приложения.
            candidate = load_index(staging_index, settings, clock)
            candidate.close()
            _fsync_file(staging_index)
            os.replace(staging_index, index_path)
            _fsync_dir(index_path.parent)
            published = load_index(index_path, settings, clock)
            try:
                status = {
                    "ok": True,
                    "finished_at": clock.now().isoformat(),
                    "last_success_at": clock.now().isoformat(),
                    "generation": published.imported_at.isoformat(),
                    "expires_at": published.expires_at.isoformat(),
                    "workbook_sha256": report.workbook_sha256,
                    "catalog_sha256": report.catalog_sha256,
                    "doctors": report.doctors,
                    "slots": report.slots,
                    "free_slots": report.free_slots,
                    "error_code": None,
                }
            finally:
                published.close()
            return _write_status(index_path, status)
        except ScheduleImportError as error:
            _record_failure(index_path, clock, str(error))
            raise
        except MisError as error:
            _record_failure(index_path, clock, error.code)
            raise
        except Exception:
            _record_failure(index_path, clock, "REFRESH_FAILED")
            raise
        finally:
            staging_catalog.unlink(missing_ok=True)
            staging_index.unlink(missing_ok=True)


def seconds_until_due(now: datetime, expires_at: datetime | None, margin: timedelta) -> float:
    """Сколько можно ждать до плановой попытки; запас margin остаётся до конца срока."""
    if expires_at is None:
        return 0.0
    return max(0.0, (expires_at - margin - now).total_seconds())


async def refresh_loop(
    settings: Settings,
    clock: Clock,
    *,
    workbook_path: Path,
    index_path: Path,
    fetch_catalog_to: CatalogFetcher | None = None,
    catalog_path: Path | None = None,
    valid_hours: int = 24,
    margin_s: float = 7200,
    retry_s: float = 900,
    max_sleep_s: float = 3600,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    max_cycles: int | None = None,
) -> None:
    """Фоновая служба: один GET каталога на попытку и только когда обновление пора.

    Свежий индекс (в том числе опубликованный вручную) лишних запросов не вызывает:
    цикл спит до срока минус запас и перепроверяет срок не реже max_sleep_s.
    После неудачи следующая попытка не раньше retry_s.
    """
    if (
        not 1 <= valid_hours <= 168
        or not all(math.isfinite(value) for value in (margin_s, retry_s, max_sleep_s))
        or not 0 <= margin_s < valid_hours * 3600
        or retry_s < 1
        or max_sleep_s < 1
    ):
        raise ScheduleImportError("INVALID_REFRESH_INTERVAL")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        delay = seconds_until_due(
            clock.now(), index_expires_at(index_path), timedelta(seconds=margin_s)
        )
        if delay > 0:
            await sleep(min(delay, max_sleep_s))
            continue
        try:
            await refresh_once(
                settings,
                clock,
                workbook_path=workbook_path,
                index_path=index_path,
                fetch_catalog_to=fetch_catalog_to,
                catalog_path=catalog_path,
                valid_hours=valid_hours,
            )
        except Exception:
            # Статус неудачи уже записан для известных ошибок; цикл не должен
            # превращаться в плотный поток попыток и запросов к стенду.
            await sleep(retry_s)
