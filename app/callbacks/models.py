"""Задача обратного звонка и сроки по §7.6: P2 — 15 минут, P3 — 30, вне часов — до 10:00."""

from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.clock import CLINIC_TIMEZONE
from app.mis.models import MisId
from app.policies.safety import operators_on_duty

TaskState = Literal["pending", "calling", "done", "failed"]
Priority = Literal["P2", "P3"]
# §7.6: срок в рабочее время; P1 обратным звонком не заменяется (§7.5)
DELAY = {"P2": timedelta(minutes=15), "P3": timedelta(minutes=30)}
# Задачи нерабочего времени — до 10:00 следующего рабочего дня
NEXT_DAY_DEADLINE = time(10)
MAX_ATTEMPTS = 3
# Сотрудник взял задачу, но не отметил результат: столько держим её за ним
CLAIM = timedelta(minutes=10)


class CallbackTask(BookingModel):
    # Один звонок и один маршрут — одна задача: повтор возвращает ту же
    id: str = Field(min_length=1, max_length=200)
    call_id: str = Field(min_length=1, max_length=128)
    route: str = Field(pattern=r"^R-\d{2}$")
    priority: Priority
    # Карточка перевода — в заявке МИС (T08-a); здесь только ссылка на неё
    escalation_ref: str | None = None
    client_id: MisId | None = Field(default=None, repr=False)
    # Удобное время, если пациент назвал: короткая фраза, без ФИО
    preferred: str | None = Field(default=None, max_length=100, repr=False)
    created_at: AwareDatetime
    due_at: AwareDatetime
    out_of_hours: bool
    state: TaskState = "pending"
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    owner: str | None = None
    claimed_until: AwareDatetime | None = None
    # Три попытки без ответа: нужна SMS, но канала нет — флаг, а не «отправлено»
    sms_pending: bool = False
    version: int = 0


def next_working_morning(moment: datetime, holidays: frozenset[date] = frozenset()) -> datetime:
    """Ближайшие 10:00 рабочего дня после этого момента: ночью — сегодняшнее утро."""
    local = moment.astimezone(CLINIC_TIMEZONE)
    day = local.date()
    while True:
        deadline = datetime.combine(day, NEXT_DAY_DEADLINE, CLINIC_TIMEZONE)
        if deadline > local and day not in holidays and operators_on_duty(deadline):
            return deadline
        day += timedelta(days=1)


def deadline(priority: str, now: datetime, holidays: frozenset[date] = frozenset()):
    """(срок, вне часов ли). Срок, выпадающий из рабочего времени, переносится на утро."""
    if priority not in DELAY:
        raise ValueError("обратный звонок не заменяет перевод P1")
    local = now.astimezone(CLINIC_TIMEZONE)
    due = local + DELAY[priority]
    if local.date() not in holidays and operators_on_duty(local) and operators_on_duty(due):
        return due, False
    return next_working_morning(local, holidays), True


def promise(task: CallbackTask, now: datetime) -> str:
    """Срок для фразы ГА «вам перезвонят …»: без выдуманной точности."""
    if not task.out_of_hours:
        minutes = round(DELAY[task.priority].total_seconds() / 60)
        return f"в течение {minutes} минут"
    due = task.due_at.astimezone(CLINIC_TIMEZONE)
    days = (due.date() - now.astimezone(CLINIC_TIMEZONE).date()).days
    names = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу")
    when = (
        "завтра" if days == 1 else names[due.weekday()] if due.weekday() < 6 else "в рабочий день"
    )
    return f"{when} до десяти утра"
