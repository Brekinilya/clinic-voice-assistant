"""Явные предпочтения по слотам; условия времени включают обе границы."""

from datetime import date, time

from pydantic import Field

from app.booking.models import BookingModel, Slot
from app.mis.models import MisId


class SlotPreferences(BookingModel):
    date_from: date | None = None
    date_to: date | None = None
    time_from: time | None = None
    time_to: time | None = None
    time_pref: str | None = None
    exclude_weekdays: tuple[int, ...] = Field(default=(), max_length=7)
    doctor_ids: tuple[MisId, ...] = ()

    def matches(self, slot: Slot) -> bool:
        day, at = slot.scheduled_at.date(), slot.scheduled_at.time()
        return (
            (self.date_from is None or day >= self.date_from)
            and (self.date_to is None or day <= self.date_to)
            and (self.time_from is None or at >= self.time_from)
            and (self.time_to is None or at <= self.time_to)
            and day.isoweekday() not in self.exclude_weekdays
            and (not self.doctor_ids or slot.doctor_id in self.doctor_ids)
        )
