"""Задания и попытки напоминаний: только технические ссылки, без ФИО и телефона в журнале."""

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.mis.models import MisId
from app.visits.models import VisitSummary

# С-10: до двух попыток с интервалом не менее двух часов
MAX_ATTEMPTS = 2
RETRY_AFTER = timedelta(hours=2)
# Запись, созданная менее суток назад, напоминания не получает
FRESH_VISIT = timedelta(hours=24)


class Outcome(StrEnum):
    """Код результата обзвона С-10 и «не знаю», при котором запись не меняется."""

    CONFIRMED = "CONFIRMED"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"
    WRONG_PERSON = "WRONG_PERSON"
    VOICEMAIL = "VOICEMAIL"
    NO_ANSWER = "NO_ANSWER"
    OPT_OUT = "OPT_OUT"
    ESCALATED = "ESCALATED"
    MAYBE = "MAYBE"


class AttemptResult(StrEnum):
    """Исход одной попытки, если разговор не дошёл до кода С-10."""

    NO_ANSWER = "NO_ANSWER"
    # Вызов не состоялся: шлюз отклонил, софтфон не в сети
    FAILED = "FAILED"
    # Ответили, но разговор оборвался без исхода
    INCOMPLETE = "INCOMPLETE"
    # Перед звонком выяснилось, что звонить не нужно
    SKIPPED = "SKIPPED"
    # Звонок не начинали: МИС не ответила при проверке визита
    ABANDONED = "ABANDONED"


# После этих исходов разговора второй попытки нет
FINAL = frozenset(Outcome) - {Outcome.NO_ANSWER}
RETRYABLE = frozenset({AttemptResult.NO_ANSWER, AttemptResult.FAILED, AttemptResult.INCOMPLETE})
# Пациента не застали: регламент требует SMS-напоминания
NEEDS_SMS = frozenset({Outcome.NO_ANSWER, Outcome.WRONG_PERSON})

JobState = Literal["planned", "calling", "waiting_retry", "done", "skipped", "manual"]
AttemptState = Literal["claimed", "dialing", "answered", "finished", "unknown", "abandoned"]
SkipReason = Literal[
    "created_recently",
    "no_contact",
    "endpoint_not_allowed",
    "opt_out",
    "already_confirmed",
    "changed",
]


def new_id() -> str:
    return uuid4().hex


class ReminderJob(BookingModel):
    id: str = Field(default_factory=new_id)
    visit_id: MisId
    client_id: MisId = Field(repr=False)
    scheduled_at: AwareDatetime
    call_day: date
    state: JobState = "planned"
    next_at: AwareDatetime
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    outcome: str | None = None
    reason: str | None = None
    # SMS-напоминание нужно, но канала доставки пока нет: флаг виден, отправленным не считается
    sms_pending: bool = False
    version: int = 0


class ReminderAttempt(BookingModel):
    id: str = Field(default_factory=new_id)
    job_id: str
    number: int = Field(ge=1, le=MAX_ATTEMPTS)
    owner: str
    lease_until: AwareDatetime
    state: AttemptState = "claimed"
    result: str | None = None
    reason: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class Contact(BookingModel):
    """Куда звонить: в карточке клиента стенда телефона нет, справочник локальный."""

    client_id: MisId = Field(repr=False)
    endpoint: str = Field(min_length=1, max_length=200, repr=False)
    opt_out_at: AwareDatetime | None = None


class OutboundContext(BookingModel):
    """С чем исходящий звонок входит в диалог. Полномочия звонка — только этот визит."""

    attempt_id: str
    purpose: Literal["reminder"] = "reminder"
    client_id: MisId = Field(repr=False)
    visit: VisitSummary
    # Только имя и отчество: до подтверждения адресата фамилия не звучит (раздел 3.5)
    addressee: str = Field(min_length=1, max_length=201, repr=False)


def after_attempt(
    job: ReminderJob,
    number: int,
    result: str,
    reason: str | None,
    now: datetime,
    retry_window_end: datetime | None,
) -> dict:
    """Новое состояние задания после попытки. retry_window_end — конец окна дня звонка."""
    if result in FINAL:
        return {
            "state": "done",
            "outcome": result,
            "reason": reason,
            "sms_pending": result in NEEDS_SMS,
            "next_at": now,
        }
    if result == AttemptResult.SKIPPED:
        return {"state": "skipped", "reason": reason, "next_at": now}
    if result == AttemptResult.ABANDONED:
        # Попытку не засчитываем: звонка не было
        return {
            "state": "planned" if number == 1 else "waiting_retry",
            "attempts": number - 1,
            "reason": reason,
            "next_at": now + timedelta(minutes=5),
        }
    retry_at = now + RETRY_AFTER
    if (
        result in RETRYABLE
        and number < MAX_ATTEMPTS
        and retry_window_end is not None
        and retry_at < retry_window_end
    ):
        return {"state": "waiting_retry", "reason": reason or result, "next_at": retry_at}
    return {
        "state": "done",
        "outcome": Outcome.NO_ANSWER,
        "reason": reason or result,
        "sms_pending": True,
        "next_at": now,
    }
