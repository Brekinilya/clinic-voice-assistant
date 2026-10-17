"""Единый источник текущего времени для звонков и относительных дат."""

from datetime import datetime, timedelta, timezone

CLINIC_TIMEZONE = timezone(timedelta(hours=4), name="GMT+4")


class ClinicClock:
    def now(self) -> datetime:
        return datetime.now(CLINIC_TIMEZONE)
