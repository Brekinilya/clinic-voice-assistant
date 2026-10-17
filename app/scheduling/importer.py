"""Атомарная публикация индекса после полной проверки книги и каталога."""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.scheduling.matching import match_doctors, profile_key
from app.scheduling.models import Catalog, ImportReport, ScheduleImportError, SlotStatus
from app.scheduling.workbook import WorkbookSource

SCHEMA = """
CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE doctors (id INTEGER PRIMARY KEY, excel_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, specialty TEXT NOT NULL, profile TEXT NOT NULL,
  patient_group TEXT NOT NULL, room TEXT NOT NULL, minutes INTEGER NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)));
CREATE TABLE slots (source_id TEXT PRIMARY KEY, doctor_id INTEGER NOT NULL REFERENCES doctors(id),
  day TEXT NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL,
  status TEXT NOT NULL, UNIQUE(doctor_id,day,start));
CREATE INDEX slot_search ON slots(day,status,doctor_id,start);
CREATE INDEX doctor_profile ON doctors(profile,id);
PRAGMA user_version=1;
"""


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def import_workbook(
    source: Path,
    catalog: Catalog,
    destination: Path,
    now: datetime,
    *,
    replace: bool = False,
    valid_hours: int = 24,
) -> ImportReport:
    if not 1 <= valid_hours <= 168 or now.tzinfo is None or catalog.captured_at > now:
        raise ScheduleImportError("INVALID_IMPORT_TIME")
    if source.resolve() == destination.resolve() or (destination.exists() and not replace):
        raise ScheduleImportError("DESTINATION_EXISTS")
    source_hash = digest(source)
    catalog_json = catalog.model_dump_json()
    catalog_hash = hashlib.sha256(catalog_json.encode()).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".schedule-", suffix=".sqlite", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with WorkbookSource(source) as workbook, sqlite3.connect(temporary) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            matched = match_doctors(tuple(workbook.doctors.values()), catalog.doctors)
            for key, doctor in matched.items():
                original = workbook.doctors[key]
                db.execute(
                    "INSERT INTO doctors VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        doctor.id,
                        key,
                        doctor.full_name,
                        doctor.specialty,
                        profile_key(doctor.specialty),
                        original.patient_group,
                        original.room,
                        original.minutes,
                        int(doctor.is_active),
                    ),
                )
            count, free = 0, 0
            for slot in workbook.slots():
                db.execute(
                    "INSERT INTO slots VALUES (?,?,?,?,?,?)",
                    (
                        slot.source_id,
                        matched[slot.excel_doctor].id,
                        slot.at.date().isoformat(),
                        slot.at.time().isoformat(timespec="minutes"),
                        slot.end.time().isoformat(timespec="minutes"),
                        slot.status,
                    ),
                )
                count += 1
                free += slot.status == SlotStatus.FREE
            if not count:
                raise ScheduleImportError("EMPTY_SLOTS")
            overlap = db.execute("""SELECT 1 FROM (SELECT start,lag(finish) OVER
                (PARTITION BY doctor_id,day ORDER BY start) AS previous FROM slots)
                WHERE previous > start LIMIT 1""").fetchone()
            if overlap:
                raise ScheduleImportError("OVERLAPPING_SLOTS")
            report = ImportReport(
                workbook_sha256=source_hash,
                catalog_sha256=catalog_hash,
                mode=catalog.mode,
                doctors=len(matched),
                slots=count,
                free_slots=free,
                date_from=workbook.start,
                date_to=workbook.end,
            )
            metadata = {
                "catalog": catalog.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
                "imported_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=valid_hours)).isoformat(),
            }
            db.execute(
                "INSERT INTO metadata VALUES (1,?)", (json.dumps(metadata, ensure_ascii=False),)
            )
            if (
                db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchone()
            ):
                raise ScheduleImportError("INVALID_INDEX")
        if digest(source) != source_hash:
            raise ScheduleImportError("WORKBOOK_CHANGED_DURING_IMPORT")
        # На Windows соединение надо закрыть до публикации файла.
        db.close()
        if replace:
            os.replace(temporary, destination)
        else:
            # Публикация без перезаписи даже при гонке двух независимых импортов.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise ScheduleImportError("DESTINATION_EXISTS") from None
        return report
    except sqlite3.IntegrityError:
        raise ScheduleImportError("DUPLICATE_SLOT") from None
    finally:
        if "db" in locals():
            db.close()
        temporary.unlink(missing_ok=True)
