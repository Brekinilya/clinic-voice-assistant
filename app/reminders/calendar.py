"""Когда звонить по С-10: за один рабочий день до приёма и только в окна обзвона.

Всё считается во времени клиники (GMT+4), а не в часовом поясе компьютера.
"""

from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from app.clock import CLINIC_TIMEZONE
from app.resource_paths import config_directory

# Будни 10:00–19:00, суббота 10:00–15:00, в воскресенье и праздники не звоним
WINDOWS = {**{day: (time(10), time(19)) for day in range(5)}, 5: (time(10), time(15))}


class ClinicCalendar:
    def __init__(
        self,
        holidays: frozenset[date] = frozenset(),
        covered: tuple[date, date] = (date.min, date.max),
    ):
        self.holidays = holidays
        self.covered = covered

    @classmethod
    def load(cls, path: Path | None = None) -> "ClinicCalendar":
        data = yaml.safe_load((path or config_directory() / "calendar.yaml").read_text("utf-8"))
        period = data["covered"]
        return cls(
            frozenset(date.fromisoformat(str(day)) for day in data.get("holidays") or ()),
            (date.fromisoformat(str(period["from"])), date.fromisoformat(str(period["to"]))),
        )

    def covers(self, day: date) -> bool:
        return self.covered[0] <= day <= self.covered[1]

    def working(self, day: date) -> bool:
        """Рабочий день для правила «за один рабочий день»: понедельник–пятница, не праздник."""
        return day.weekday() < 5 and day not in self.holidays

    def window(self, day: date) -> tuple[datetime, datetime] | None:
        hours = WINDOWS.get(day.weekday())
        # Вне сверенного периода не звоним: праздник мог быть неизвестен
        if hours is None or day in self.holidays or not self.covers(day):
            return None
        start, end = hours
        return (
            datetime.combine(day, start, CLINIC_TIMEZONE),
            datetime.combine(day, end, CLINIC_TIMEZONE),
        )

    def is_open(self, moment: datetime) -> bool:
        local = moment.astimezone(CLINIC_TIMEZONE)
        window = self.window(local.date())
        return window is not None and window[0] <= local < window[1]

    def call_day(self, visit_day: date) -> date:
        """Для приёма в понедельник — пятница; праздники пропускаются."""
        day = visit_day - timedelta(days=1)
        while not self.working(day):
            day -= timedelta(days=1)
        return day

    def visit_days(self, call_day: date) -> list[date]:
        """Дни приёма, о которых напоминают в этот день звонка."""
        return [
            call_day + timedelta(days=n)
            for n in range(1, 15)
            if self.call_day(call_day + timedelta(days=n)) == call_day
        ]
