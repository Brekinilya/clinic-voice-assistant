"""Потоковое чтение исходных листов, без пересчёта сводных формул и изменения XLSX."""

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

from app.clock import CLINIC_TIMEZONE
from app.mis.models import name_key
from app.scheduling.access import PATIENT_GROUPS
from app.scheduling.models import ScheduleImportError, SlotStatus, WorkbookDoctor

DOCTOR_HEADER = (
    "ID",
    "ФИО",
    "Отделение",
    "Специальность (профиль)",
    "Пациенты",
    "Кабинет",
    "Слот, мин",
    "График",
)
SLOT_HEADER = (
    "ID слота",
    "Дата",
    "День",
    "ID врача",
    "ФИО врача",
    "Специальность",
    "Отделение",
    "Кабинет",
    "Начало",
    "Окончание",
    "Мин",
    "Статус",
)


def day(value) -> date:
    if not isinstance(value, datetime) or value.time() != time(0):
        raise ScheduleImportError("INVALID_WORKBOOK_DATE")
    return value.date()


def text(value) -> str:
    if not isinstance(value, str) or not value.strip() or value.startswith("="):
        raise ScheduleImportError("INVALID_WORKBOOK_TEXT")
    return value.strip()


def room(value) -> str:
    if isinstance(value, str) and value.strip() and not value.startswith("="):
        return value.strip()
    if type(value) is int and value > 0:
        return str(value)
    raise ScheduleImportError("INVALID_ROOM")


@dataclass(frozen=True)
class RawSlot:
    source_id: str
    excel_doctor: str
    at: datetime
    end: datetime
    status: SlotStatus


class WorkbookSource:
    def __init__(self, path: Path):
        try:
            if path.stat().st_size > 64_000_000:
                raise ScheduleImportError("WORKBOOK_TOO_LARGE")
            with ZipFile(path) as archive:
                if sum(i.file_size for i in archive.infolist()) > 512_000_000:
                    raise ScheduleImportError("WORKBOOK_TOO_LARGE")
            self.book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        except (OSError, BadZipFile, ValueError, KeyError):
            raise ScheduleImportError("INVALID_WORKBOOK") from None

    def __enter__(self):
        try:
            if "Описание" not in self.book.sheetnames:
                raise ScheduleImportError("MISSING_WORKBOOK_SHEET")
            self.start = day(self.book["Описание"]["B4"].value)
            self.end = day(self.book["Описание"]["B6"].value)
            if self.end < self.start or (self.end - self.start).days > 366:
                raise ScheduleImportError("INVALID_PERIOD")
            self.doctors = self._doctors()
            self.calendar = self._calendar()
            self.absences = self._absences()
            return self
        except Exception:
            self.book.close()
            raise

    def __exit__(self, *args):
        self.book.close()

    def _rows(self, sheet, columns):
        try:
            ws = self.book[sheet]
        except KeyError:
            raise ScheduleImportError("MISSING_WORKBOOK_SHEET") from None
        # Размеры могут быть неверны в стороннем редакторе; читаем фактические строки.
        ws.reset_dimensions()
        return ws.iter_rows(max_col=columns, values_only=True)

    def _doctors(self):
        rows = self._rows("Врачи", 8)
        if tuple(next(rows, ())) != DOCTOR_HEADER:
            raise ScheduleImportError("INVALID_DOCTOR_HEADER")
        result = {}
        for row in rows:
            if not any(v is not None for v in row):
                continue
            identifier, name, department, specialty, group, cabinet, minutes, _ = row
            identifier = text(identifier)
            if identifier in result or len(result) >= 500:
                raise ScheduleImportError("DUPLICATE_OR_EXCESS_DOCTORS")
            if group not in PATIENT_GROUPS:
                raise ScheduleImportError("UNKNOWN_PATIENT_GROUP")
            if type(minutes) is not int:
                raise ScheduleImportError("INVALID_DURATION")
            result[identifier] = WorkbookDoctor(
                excel_id=identifier,
                full_name=text(name),
                specialty=text(specialty),
                department=text(department),
                patient_group=group,
                room=room(cabinet),
                minutes=minutes,
            )
        if not result:
            raise ScheduleImportError("EMPTY_DOCTORS")
        return result

    def _calendar(self):
        rows = self._rows("Календарь", 370)
        header = next(rows, ())
        if header[:3] != ("ID", "ФИО", "Специальность"):
            raise ScheduleImportError("INVALID_CALENDAR_HEADER")
        dates = tuple(day(v) for v in header[3:] if v is not None)
        if dates != tuple(
            self.start.fromordinal(self.start.toordinal() + i)
            for i in range((self.end - self.start).days + 1)
        ):
            raise ScheduleImportError("INCOMPLETE_CALENDAR")
        next(rows, None)  # Вторая строка — подписи дней недели.
        result, seen = {}, set()
        for row in rows:
            if row[0] is None:
                continue
            identifier = row[0]
            doctor = self.doctors.get(identifier)
            if (
                doctor is None
                or identifier in seen
                or name_key(text(row[1])) != name_key(doctor.full_name)
                or text(row[2]) != doctor.specialty
            ):
                raise ScheduleImportError("INVALID_CALENDAR_DOCTOR")
            seen.add(identifier)
            for value, code in zip(dates, row[3:]):
                if code not in {"Р", "В", "О", "Б", "У", "П"}:
                    raise ScheduleImportError("INVALID_CALENDAR_CODE")
                result[(identifier, value)] = code
        if seen != set(self.doctors):
            raise ScheduleImportError("INCOMPLETE_CALENDAR")
        return result

    def _absences(self):
        rows = self._rows("Недоступность", 6)
        if next(rows, ()) != ("ID врача", "ФИО врача", "Специальность", "Тип", "С", "По"):
            raise ScheduleImportError("INVALID_ABSENCE_HEADER")
        result = {key: [] for key in self.doctors}
        for row in rows:
            if row[0] is None:
                continue
            identifier, name, specialty, kind, first, last = row
            doctor = self.doctors.get(identifier)
            first, last = day(first), day(last)
            if (
                doctor is None
                or name_key(text(name)) != name_key(doctor.full_name)
                or specialty != doctor.specialty
                or kind not in {"Отпуск", "Больничный", "Обучение"}
                or last < first
            ):
                raise ScheduleImportError("INVALID_ABSENCE")
            result[identifier].append((first, last))
        return result

    def slots(self):
        rows = self._rows("Слоты", 12)
        if next(rows, ()) != SLOT_HEADER:
            raise ScheduleImportError("INVALID_SLOT_HEADER")
        for number, row in enumerate(rows, 1):
            if number > 500000:
                raise ScheduleImportError("EXCESS_SLOTS")
            if not any(v is not None for v in row):
                continue
            (
                identifier,
                value,
                _,
                doctor_id,
                name,
                specialty,
                department,
                cabinet,
                start,
                end,
                minutes,
                status,
            ) = row
            doctor = self.doctors.get(doctor_id)
            value = day(value)
            if (
                doctor is None
                or name_key(text(name)) != name_key(doctor.full_name)
                or specialty != doctor.specialty
                or department != doctor.department
                or room(cabinet) != doctor.room
                or type(minutes) is not int
                or minutes != doctor.minutes
            ):
                raise ScheduleImportError("SLOT_DOCTOR_MISMATCH")
            if (
                not isinstance(start, time)
                or not isinstance(end, time)
                or start.second
                or end.second
                or start.microsecond
                or end.microsecond
            ):
                raise ScheduleImportError("INVALID_SLOT_TIME")
            at, finish = (
                datetime.combine(value, start, CLINIC_TIMEZONE),
                datetime.combine(value, end, CLINIC_TIMEZONE),
            )
            if (finish - at).total_seconds() != doctor.minutes * 60:
                raise ScheduleImportError("INVALID_SLOT_DURATION")
            if self.calendar.get((doctor_id, value)) != "Р" or any(
                first <= value <= last for first, last in self.absences[doctor_id]
            ):
                raise ScheduleImportError("SLOT_DURING_UNAVAILABILITY")
            try:
                state = SlotStatus(status)
            except ValueError:
                raise ScheduleImportError("INVALID_SLOT_STATUS") from None
            yield RawSlot(text(identifier), doctor_id, at, finish, state)
