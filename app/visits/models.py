"""Данные справки без карточки пациента и отдельный фильтр существующих посещений."""

from datetime import datetime

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.booking.preferences import SlotPreferences
from app.mis.models import MisId, Visit
from app.scheduling.matching import profile_key


class VisitQuery(BookingModel):
    specialty: str | None = None
    doctor: str | None = None
    preferences: SlotPreferences = Field(default_factory=SlotPreferences)

    def matches(self, visit: Visit) -> bool:
        day, at = visit.scheduled_at.date(), visit.scheduled_at.time()
        p = self.preferences
        return (
            (
                self.specialty is None
                or profile_key(visit.doctor.specialty) == profile_key(self.specialty)
            )
            and (
                self.doctor is None
                or visit.doctor.full_name.split()[0].casefold() == self.doctor.casefold()
            )
            and (p.date_from is None or day >= p.date_from)
            and (p.date_to is None or day <= p.date_to)
            and (p.time_from is None or at >= p.time_from)
            and (p.time_to is None or at <= p.time_to)
            and day.isoweekday() not in p.exclude_weekdays
            and (not p.doctor_ids or visit.doctor.id in p.doctor_ids)
        )


class VisitSummary(BookingModel):
    visit_id: MisId
    doctor_id: MisId
    doctor_name: str
    specialty: str
    scheduled_at: AwareDatetime
    room: str | None = None


class VisitCursor(BookingModel):
    scheduled_at: AwareDatetime
    visit_id: MisId

    def key(self) -> tuple[datetime, int]:
        return self.scheduled_at, self.visit_id


class VisitPage(BookingModel):
    items: tuple[VisitSummary, ...] = Field(default=(), max_length=3)
    has_more: bool = False
    cursor: VisitCursor | None = None
