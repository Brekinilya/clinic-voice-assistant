"""Неизменяемый SQLite-справочник: короткие запросы и кабинеты, без записей пациентов."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.booking.models import Slot
from app.booking.preferences import SlotPreferences
from app.booking.schedule import AccessFact, AccessState
from app.clock import CLINIC_TIMEZONE
from app.mis.models import Client, Doctor, name_key
from app.ports import Clock
from app.scheduling.access import PATIENT_GROUPS, access_state
from app.scheduling.matching import profile_key
from app.scheduling.models import Catalog, ImportReport, ScheduleImportError


class SqliteSchedule:
    def __init__(
        self,
        path: Path,
        clock: Clock,
        *,
        mode: str,
        base_url: str | None = None,
        token_scope: str | None = None,
        facts: tuple[AccessFact, ...] = (),
    ):
        self.clock = clock
        self._patients = {}
        self._db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        try:
            if self._db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ScheduleImportError("UNSUPPORTED_INDEX")
            record = self._db.execute("SELECT data FROM metadata WHERE id=1").fetchone()
            metadata = json.loads(record[0])
            self.catalog = Catalog.model_validate(metadata["catalog"])
            self.report = ImportReport.model_validate(metadata["report"])
            if (
                self.report.mode != self.catalog.mode
                or hashlib.sha256(self.catalog.model_dump_json().encode()).hexdigest()
                != self.report.catalog_sha256
            ):
                raise ScheduleImportError("INDEX_CATALOG_MISMATCH")
            self.imported_at = datetime.fromisoformat(metadata["imported_at"])
            self.expires_at = datetime.fromisoformat(metadata["expires_at"])
            if (
                self.imported_at.tzinfo is None
                or self.expires_at.tzinfo is None
                or self.expires_at <= self.imported_at
            ):
                raise ScheduleImportError("INVALID_INDEX_TIME")
            self.check_scope(mode, base_url, token_scope)
            self._doctors = {row["id"]: row for row in self._db.execute("SELECT * FROM doctors")}
            expected = {d.id: d for d in self.catalog.doctors}
            for identifier, row in self._doctors.items():
                doctor = expected.get(identifier)
                if (
                    doctor is None
                    or row["name"] != doctor.full_name
                    or row["specialty"] != doctor.specialty
                    or bool(row["active"]) != doctor.is_active
                    or row["profile"] != profile_key(doctor.specialty)
                    or row["patient_group"] not in PATIENT_GROUPS
                ):
                    raise ScheduleImportError("INDEX_DOCTOR_MISMATCH")
            if len(self._doctors) != self.report.doctors:
                raise ScheduleImportError("INDEX_DOCTOR_MISMATCH")
            self.facts = self._validate_facts(facts)
            self.fingerprint = self.report.workbook_sha256
            self._check_current()
        except Exception:
            self._db.close()
            raise

    def close(self):
        self._db.close()

    def check_scope(self, mode: str, base_url: str | None, token_scope: str | None):
        if self.catalog.mode != mode or (
            mode == "hackathon"
            and (
                self.catalog.base_url.rstrip("/") != (base_url or "").rstrip("/")
                or self.catalog.token_scope != token_scope
            )
        ):
            raise ScheduleImportError("INDEX_SCOPE_MISMATCH")

    def _check_current(self):
        now = self.clock.now()
        if (
            not self.imported_at <= now < self.expires_at
            or not self.report.date_from
            <= now.astimezone(CLINIC_TIMEZONE).date()
            <= self.report.date_to
        ):
            raise ScheduleImportError("SCHEDULE_NOT_CURRENT")

    def _validate_facts(self, facts):
        values = {}
        for fact in facts:
            if fact.doctor_id not in self._doctors:
                raise ScheduleImportError("ACCESS_UNKNOWN_DOCTOR")
            values.setdefault((fact.client_id, fact.doctor_id), []).append(fact)
        for group in values.values():
            group.sort(key=lambda f: f.valid_from)
            if any(a.valid_until >= b.valid_from for a, b in zip(group, group[1:])):
                raise ScheduleImportError("ACCESS_OVERLAP")
        return tuple(facts)

    @property
    def doctors(self):
        return tuple(d for d in self.catalog.doctors if d.id in self._doctors)

    def specialties_for(self, specialty: str) -> tuple[str, ...]:
        self._check_current()
        return tuple(
            sorted(
                {
                    d.specialty
                    for d in self.doctors
                    if profile_key(d.specialty) == profile_key(specialty)
                }
            )
        )

    def remember_patient(self, client: Client):
        self._patients.pop(client.id, None)
        if len(self._patients) >= 128:
            self._patients.pop(next(iter(self._patients)))
        # Нужна только дата рождения; ФИО и комментарий в этот кэш не входят.
        self._patients[client.id] = client.birth_date

    def matches_doctor(self, doctor: Doctor):
        self._check_current()
        row = self._doctors.get(doctor.id)
        return bool(
            row
            and doctor.is_active
            and name_key(row["name"]) == name_key(doctor.full_name)
            and profile_key(row["specialty"]) == profile_key(doctor.specialty)
        )

    def _row(self, slot: Slot):
        self._check_current()
        local = slot.scheduled_at.astimezone(CLINIC_TIMEZONE)
        if local.second or local.microsecond:
            return None
        return self._db.execute(
            """SELECT s.*,d.specialty,d.patient_group,d.room,d.active FROM slots s
          JOIN doctors d ON d.id=s.doctor_id WHERE s.doctor_id=? AND s.day=? AND s.start=?""",
            (slot.doctor_id, local.date().isoformat(), local.time().isoformat(timespec="minutes")),
        ).fetchone()

    def contains(self, slot: Slot):
        row = self._row(slot)
        return bool(
            row
            and row["status"] == "Свободен"
            and row["active"]
            and row["specialty"] == slot.specialty
        )

    def eligible(self, client_id: int, slot: Slot):
        row = self._row(slot)
        if not row or not self.contains(slot):
            return False
        return (
            access_state(
                self._patients.get(client_id),
                slot.doctor_id,
                slot.specialty,
                row["patient_group"],
                slot.scheduled_at.astimezone(CLINIC_TIMEZONE),
                client_id,
                self.facts,
            )
            == AccessState.ALLOWED
        )

    def room(self, doctor_id: int, scheduled_at: datetime):
        self._check_current()
        at = scheduled_at.astimezone(CLINIC_TIMEZONE)
        if at.second or at.microsecond:
            return None
        row = self._db.execute(
            """SELECT d.room FROM slots s JOIN doctors d ON d.id=s.doctor_id
          WHERE s.doctor_id=? AND s.day=? AND s.start=?""",
            (doctor_id, at.date().isoformat(), at.time().isoformat(timespec="minutes")),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _slot(row):
        return Slot(
            doctor_id=row["doctor_id"],
            specialty=row["specialty"],
            scheduled_at=datetime.fromisoformat(row["day"] + "T" + row["start"]).replace(
                tzinfo=CLINIC_TIMEZONE
            ),
        )

    def slots(self):
        self._check_current()
        return tuple(
            self._slot(row)
            for row in self._db.execute("""SELECT s.*,d.specialty
            FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE s.status='Свободен' AND d.active=1""")
        )

    def find(self, specialty: str, preferences: SlotPreferences, now: datetime):
        self._check_current()
        now = now.astimezone(CLINIC_TIMEZONE)
        first = max(now.date(), preferences.date_from or now.date())
        last = min(
            now.date() + timedelta(days=13), preferences.date_to or now.date() + timedelta(days=13)
        )
        if first > last:
            return ()
        conditions = ["d.profile=?", "s.day BETWEEN ? AND ?", "s.status='Свободен'", "d.active=1"]
        params = [profile_key(specialty), first.isoformat(), last.isoformat()]
        if preferences.doctor_ids:
            conditions.append("d.id IN (" + ",".join("?" for _ in preferences.doctor_ids) + ")")
            params.extend(preferences.doctor_ids)
        if preferences.time_from:
            conditions.append("s.start||':00'>=?")
            params.append(preferences.time_from.isoformat(timespec="seconds"))
        if preferences.time_to:
            conditions.append("s.start||':00'<=?")
            params.append(preferences.time_to.isoformat(timespec="seconds"))
        order = "DESC" if preferences.time_pref == "late" else "ASC"
        rows = self._db.execute(
            "SELECT s.*,d.specialty FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE "
            + " AND ".join(conditions)
            + f" ORDER BY s.day {order},s.start {order},s.doctor_id {order}",
            params,
        )
        result = []
        for row in rows:
            slot = self._slot(row)
            if preferences.matches(slot) and slot.scheduled_at >= now + timedelta(minutes=30):
                result.append(slot)
            if len(result) > 20000:
                raise ScheduleImportError("QUERY_TOO_LARGE")
        return tuple(result)
