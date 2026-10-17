"""Кабинеты берутся из отдельного подтверждённого справочника, не выводятся из ID врача."""

from datetime import datetime
from typing import Protocol


class DoctorRooms(Protocol):
    def room(self, doctor_id: int, scheduled_at: datetime) -> str | None: ...
