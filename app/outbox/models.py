"""Типизированные события без телефона, ФИО, даты рождения и текста причины."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.cancellation.models import CancelAction
from app.mis.models import MisId, Visit

MAX_ATTEMPTS = 5


class OutboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EventKind(StrEnum):
    CANCELLATION_SMS = "cancellation_sms"
    SLOT_RELEASED = "slot_released"
    RESCHEDULE_SMS = "reschedule_sms"
    REMINDER_SMS = "reminder_sms"


class CancellationSms(OutboxModel):
    kind: Literal[EventKind.CANCELLATION_SMS] = EventKind.CANCELLATION_SMS
    client_id: MisId = Field(repr=False)
    visit_id: MisId
    scheduled_at: AwareDatetime


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


class ReminderSms(OutboxModel):
    kind: Literal[EventKind.REMINDER_SMS] = EventKind.REMINDER_SMS
    client_id: MisId = Field(repr=False)
    scheduled_at: AwareDatetime
    room: str | None = None


Payload = Annotated[
    CancellationSms | ReleasedSlot | RescheduleSms | ReminderSms,
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
    mode: Literal["demo_journal", "provider_accepted"]
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
    source_id: str, client_id: MisId, scheduled_at: datetime, room: str | None, now: datetime
) -> OutboxJob:
    payload = ReminderSms(client_id=client_id, scheduled_at=scheduled_at, room=room)
    return OutboxJob(
        event_id=event_id(source_id, payload.kind),
        source_id=source_id,
        payload=payload,
        created_at=now,
        available_at=now,
    )


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
