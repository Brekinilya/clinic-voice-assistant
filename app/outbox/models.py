"""Типизированные события без телефона, ФИО, даты рождения и текста причины."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.cancellation.models import CancelAction
from app.clock import CLINIC_TIMEZONE
from app.mis.models import MisId, Visit

MAX_ATTEMPTS = 5


class OutboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EventKind(StrEnum):
    APPOINTMENT_SMS = "appointment_sms"
    CANCELLATION_SMS = "cancellation_sms"
    SLOT_RELEASED = "slot_released"
    RESCHEDULE_SMS = "reschedule_sms"
    REMINDER_SMS = "reminder_sms"
    WAITLIST_SMS = "waitlist_sms"
    NO_ANSWER_SMS = "no_answer_sms"


class CancellationSms(OutboxModel):
    kind: Literal[EventKind.CANCELLATION_SMS] = EventKind.CANCELLATION_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    scheduled_at: AwareDatetime
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


class AppointmentSms(OutboxModel):
    kind: Literal[EventKind.APPOINTMENT_SMS] = EventKind.APPOINTMENT_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    scheduled_at: AwareDatetime
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


class ReleasedSlot(OutboxModel):
    kind: Literal[EventKind.SLOT_RELEASED] = EventKind.SLOT_RELEASED
    visit_id: MisId
    doctor_id: MisId
    specialty: str = Field(min_length=1, max_length=100)
    scheduled_at: AwareDatetime


class RescheduleSms(OutboxModel):
    kind: Literal[EventKind.RESCHEDULE_SMS] = EventKind.RESCHEDULE_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    old_visit_id: MisId
    scheduled_at: AwareDatetime
    doctor_name: str
    room: str | None = None
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


class ReminderSms(OutboxModel):
    kind: Literal[EventKind.REMINDER_SMS] = EventKind.REMINDER_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    scheduled_at: AwareDatetime
    room: str | None = None
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


class WaitlistSms(OutboxModel):
    kind: Literal[EventKind.WAITLIST_SMS] = EventKind.WAITLIST_SMS
    client_id: MisId = Field(repr=False)
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


class NoAnswerSms(OutboxModel):
    kind: Literal[EventKind.NO_ANSWER_SMS] = EventKind.NO_ANSWER_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    dispatched_at: AwareDatetime | None = None
    expected_line: str | None = Field(default=None, max_length=200, repr=False)


Payload = Annotated[
    AppointmentSms
    | CancellationSms
    | ReleasedSlot
    | RescheduleSms
    | ReminderSms
    | WaitlistSms
    | NoAnswerSms,
    Field(discriminator="kind"),
]


class JobState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class DeliveryReceipt(OutboxModel):
    event_id: str = Field(min_length=32, max_length=32)
    # Приём провайдером тоже не доказывает фактическую доставку SMS пациенту.
    mode: Literal["demo_journal", "provider_accepted", "client_comment"]
    reference: str = Field(min_length=1, max_length=160)


class OutboxJob(OutboxModel):
    event_id: str = Field(min_length=32, max_length=32)
    source_id: str = Field(min_length=1, max_length=128)
    payload: Payload = Field(repr=False)
    created_at: AwareDatetime
    available_at: AwareDatetime
    state: JobState = JobState.PENDING
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    lease_token: str | None = None
    lease_until: AwareDatetime | None = None
    last_error: str | None = None
    receipt: DeliveryReceipt | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.event_id != event_id(self.source_id, self.payload.kind):
            raise ValueError("неверный ключ события")
        if (self.state == JobState.PROCESSING) != (
            self.lease_token is not None and self.lease_until is not None
        ) or ((self.lease_token is None) != (self.lease_until is None)):
            raise ValueError("неверная аренда задания")
        if (self.state == JobState.ACKNOWLEDGED) != (self.receipt is not None):
            raise ValueError("неверная квитанция задания")
        if self.receipt is not None and self.receipt.event_id != self.event_id:
            raise ValueError("квитанция другого события")
        return self


class JournalEntry(OutboxModel):
    event_id: str
    payload: Payload = Field(repr=False)
    recorded_at: AwareDatetime
    # Текст есть только у демонстрационного SMS; телефон не придумываем.
    text: str | None = Field(default=None, repr=False)


class OutboxError(Exception):
    pass


def event_id(source_id: str, kind: EventKind) -> str:
    source = "reminder" if kind == EventKind.REMINDER_SMS else "cancellation"
    return uuid5(NAMESPACE_URL, f"clinic/{source}/{source_id}/{kind}").hex


def reminder_sms_event(
    source_id: str,
    client_id: MisId,
    visit_id: MisId,
    scheduled_at: datetime,
    room: str | None,
    now: datetime,
) -> OutboxJob:
    payload = ReminderSms(
        client_id=client_id, visit_id=visit_id, scheduled_at=scheduled_at, room=room
    )
    return OutboxJob(
        event_id=event_id(source_id, payload.kind),
        source_id=source_id,
        payload=payload,
        created_at=now,
        available_at=now,
    )


def appointment_sms_event(
    source_id: str, client_id: MisId, visit_id: MisId, scheduled_at: datetime, now: datetime
) -> OutboxJob:
    payload = AppointmentSms(client_id=client_id, visit_id=visit_id, scheduled_at=scheduled_at)
    return OutboxJob(
        event_id=event_id(source_id, payload.kind),
        source_id=source_id,
        payload=payload,
        created_at=now,
        available_at=now,
    )


def waitlist_sms_event(source_id: str, client_id: MisId, now: datetime) -> OutboxJob:
    payload = WaitlistSms(client_id=client_id)
    return OutboxJob(
        event_id=event_id(source_id, payload.kind),
        source_id=source_id,
        payload=payload,
        created_at=now,
        available_at=now,
    )


def no_answer_sms_event(
    source_id: str, client_id: MisId, visit_id: MisId, now: datetime
) -> OutboxJob:
    payload = NoAnswerSms(client_id=client_id, visit_id=visit_id)
    return OutboxJob(
        event_id=event_id(source_id, payload.kind),
        source_id=source_id,
        payload=payload,
        created_at=now,
        available_at=now,
    )


def prepare_comment_event(job: OutboxJob, now: datetime) -> OutboxJob:
    """Freeze the exact mark before the first external request; retries reuse it."""
    payload = job.payload
    if isinstance(payload, ReleasedSlot):
        return job
    if payload.expected_line is not None:
        if payload.dispatched_at is None:
            raise OutboxError("COMMENT_MARK_CORRUPT")
        return job
    if payload.dispatched_at is not None:
        raise OutboxError("COMMENT_MARK_CORRUPT")
    label = {
        EventKind.APPOINTMENT_SMS: "Запись создана",
        EventKind.CANCELLATION_SMS: "Отмена",
        EventKind.RESCHEDULE_SMS: "Перенос",
        EventKind.REMINDER_SMS: "Напоминание",
        EventKind.WAITLIST_SMS: "Лист ожидания",
        EventKind.NO_ANSWER_SMS: "Не дозвонились",
    }.get(payload.kind)
    if label is None:
        return job
    at = now.astimezone(CLINIC_TIMEZONE)
    visit_id = getattr(payload, "visit_id", None)
    visit_ref = "" if visit_id is None else f" #{visit_id}"
    line = f"SMS {label}{visit_ref} {at:%d.%m.%Y %H:%M}"
    updated = payload.model_copy(update={"dispatched_at": now, "expected_line": line})
    return OutboxJob.model_validate({**job.model_dump(), "payload": updated.model_dump()})


def cancellation_events(
    action: CancelAction, now: datetime, *, new_visit: Visit | None = None, room=None
) -> tuple[OutboxJob, OutboxJob]:
    visit = action.before
    payloads = (
        RescheduleSms(
            client_id=visit.client_id,
            visit_id=new_visit.id,
            old_visit_id=visit.id,
            scheduled_at=new_visit.scheduled_at,
            doctor_name=new_visit.doctor.full_name,
            room=room,
        )
        if new_visit is not None
        else CancellationSms(
            client_id=visit.client_id, visit_id=visit.id, scheduled_at=visit.scheduled_at
        ),
        ReleasedSlot(
            visit_id=visit.id,
            doctor_id=visit.doctor.id,
            specialty=visit.doctor.specialty,
            scheduled_at=visit.scheduled_at,
        ),
    )
    if (action.reason == "reschedule") != (new_visit is not None):
        raise OutboxError("RESCHEDULE_TARGET_REQUIRED")
    return tuple(
        OutboxJob(
            event_id=event_id(action.action_id, payload.kind),
            source_id=action.action_id,
            payload=payload,
            created_at=now,
            available_at=now,
        )
        for payload in payloads
    )
