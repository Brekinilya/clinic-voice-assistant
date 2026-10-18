"""Задания и попытки напоминаний: только технические ссылки, без ФИО и телефона в журнале."""

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.mis.models import MisId

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
RETRYABLE = frozenset({AttemptResult.NO_ANSWER, AttemptResult.FAILED})
# Пациента не застали: регламент требует SMS-напоминания
NEEDS_SMS = frozenset({Outcome.NO_ANSWER, Outcome.WRONG_PERSON})

JobState = Literal["planned", "calling", "waiting_retry", "done", "skipped", "manual"]
AttemptState = Literal["claimed", "dialing", "answered", "finished", "unknown", "abandoned"]
# Фиксация исхода в МИС: намерение пациента и подтверждённая запись — разные факты
MisStatus = Literal["not_required", "applied", "pending", "manual"]
SkipReason = Literal[
    "created_recently",
    "no_contact",
    "endpoint_not_allowed",
    "opt_out",
    "already_confirmed",
    "changed",
    "calendar_not_covered",
]


def new_id() -> str:
    return uuid4().hex


class ReminderJob(BookingModel):
    id: str = Field(default_factory=new_id)
    visit_id: MisId
    client_id: MisId = Field(repr=False)
    # Отпечаток визита: тот же visit_id с другим временем или врачом — уже другое задание
    doctor_id: MisId
    scheduled_at: AwareDatetime
    call_day: date
    state: JobState = "planned"
    next_at: AwareDatetime
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    outcome: str | None = None
    mis_status: MisStatus | None = None
    reason: str | None = None
    # SMS-напоминание нужно, но канала доставки пока нет: флаг виден, отправленным не считается
    sms_pending: bool = False
    version: int = 0


class ReminderAttempt(BookingModel):
    id: str = Field(default_factory=new_id)
    job_id: str
    number: int = Field(ge=1, le=MAX_ATTEMPTS)
    # Растёт с каждой попыткой задания; диалог проверяет его перед входом и изменением
    fence: int = Field(ge=1)
    owner: str
    lease_until: AwareDatetime
    state: AttemptState = "claimed"
    # Канал задаём сами до originate: после сбоя по нему можно найти звонок в АТС
    channel_id: str
    result: str | None = None
    mis_status: MisStatus | None = None
    reason: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class Contact(BookingModel):
    """Куда звонить: в карточке клиента стенда телефона нет, справочник локальный."""

    client_id: MisId = Field(repr=False)
    endpoint: str = Field(min_length=1, max_length=200, repr=False)
    opt_out_at: AwareDatetime | None = None


MarkKind = Literal["confirm", "opt_out"]
MarkState = Literal["prepared", "sending", "unknown", "applied", "manual"]
# Сколько раз только читаем комментарий после неизвестного исхода PATCH, прежде чем позвать человека
MARK_CHECKS = 3
MARK_CHECK_AFTER = timedelta(minutes=1)


class ReminderMark(BookingModel):
    """Журнал одной отметки в комментарии клиента. Хранится только наша строка, не чужой текст."""

    id: str
    attempt_id: str
    kind: MarkKind
    client_id: MisId = Field(repr=False)
    visit_id: MisId | None = None
    line: str = Field(min_length=1, max_length=200)
    state: MarkState = "prepared"
    checks: int = Field(default=0, ge=0)
    reason: str | None = None
    next_check_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @property
    def mis_status(self) -> MisStatus:
        return {"applied": "applied", "manual": "manual"}.get(self.state, "pending")


def after_attempt(
    job: ReminderJob,
    number: int,
    result: str,
    reason: str | None,
    now: datetime,
    retry_window_end: datetime | None,
    mis_status: str | None = None,
) -> dict:
    """Новое состояние задания после попытки. retry_window_end — конец окна дня звонка."""
    if result == AttemptResult.INCOMPLETE:
        # Ответивший пациент мог подтвердить операцию до обрыва. Без сверки повторять нельзя.
        return {
            "state": "manual",
            "outcome": Outcome.ESCALATED,
            "mis_status": "manual",
            "reason": reason or "interrupted_conversation",
            "next_at": now,
        }
    if result in FINAL:
        # Пациент ответил, но запись в МИС не установлена: решает человек, повторного звонка нет
        return {
            "state": "manual" if mis_status == "manual" else "done",
            "outcome": result,
            "mis_status": mis_status or "not_required",
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
