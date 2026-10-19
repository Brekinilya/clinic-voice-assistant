"""Календарь регистратуры для срока callback, отдельно от напоминаний."""

from datetime import date, datetime, time, timedelta

from app.clock import CLINIC_TIMEZONE

CALENDAR_COVERAGE_START = date(2026, 1, 1)
CALENDAR_COVERAGE_END = date(2026, 12, 31)

# Праздничные и перенесённые нерабочие будни по производственному календарю РФ 2026.
VERIFIED_NONWORKING_DAYS = frozenset(
    date(2026, month, day)
    for month, days in {
        1: range(1, 12),
        2: (23,),
        3: (9,),
        5: (1, 9, 11),
        6: (12,),
        11: (4,),
        12: (31,),
    }.items()
    for day in days
)


class CalendarUnavailable(ValueError):
    """Требуемая дата вне сверенного календарного покрытия."""


def is_open_day(day: date) -> bool:
    if not CALENDAR_COVERAGE_START <= day <= CALENDAR_COVERAGE_END:
        raise CalendarUnavailable("дата вне сверенного календаря")
    return day.weekday() < 6 and day not in VERIFIED_NONWORKING_DAYS


def clinic_hours(day: date) -> tuple[time, time] | None:
    if not is_open_day(day):
        return None
    if day.weekday() == 5:
        return time(8, 30), time(15)
    return time(7), time(20)


def callback_deadline(now: datetime, priority: str) -> datetime:
    """P2=15m, P3=30m; после закрытия срок — 10:00 ближайшего рабочего дня."""
    now = now.astimezone(CLINIC_TIMEZONE)
    delta = timedelta(minutes=15 if priority == "P2" else 30)
    hours = clinic_hours(now.date())
    if hours is not None:
        opening, closing = hours
        candidate = now + delta
        if opening <= now.time() < closing and candidate.time() <= closing:
            return candidate

    day = now.date()
    while True:
        day += timedelta(days=1)
        hours = clinic_hours(day)
        if hours is not None:
            return datetime.combine(day, time(10), CLINIC_TIMEZONE)
