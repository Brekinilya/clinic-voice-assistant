"""Внутренний контракт T11. Контекст задания не заменяет согласие на действие."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from app.booking.models import BookingModel
from app.mis.models import MisId
from app.visits.models import VisitSummary

if TYPE_CHECKING:
    from app.dialogue.models import Reply

AttemptId = str
ReminderOutcome = Literal[
    "CONFIRMED",
    "RESCHEDULED",
    "CANCELLED",
    "WRONG_PERSON",
    "VOICEMAIL",
    "NO_ANSWER",
    "OPT_OUT",
    "ESCALATED",
    "MAYBE",
]


class OutboundContext(BookingModel):
    """Только из доверенного исполнителя T11, не из текста/публичного HTTP."""

    attempt_id: AttemptId = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    fence: int = Field(strict=True, ge=1)
    purpose: Literal["reminder"] = "reminder"
    client_id: MisId = Field(repr=False)
    visit: VisitSummary
    # Только имя и отчество из проверенной карточки; не сериализовать в диагностику.
    addressee: str = Field(min_length=1, max_length=201, repr=False)

    @field_validator("addressee")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("нужно имя адресата без управляющих символов")
        return value.strip()


class OutboundEvent(BookingModel):
    """Событие занимает обычный номер реплики: повтор имеет ту же квитанцию."""

    attempt_id: AttemptId = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    turn_id: int = Field(strict=True, ge=2)
    kind: Literal["voicemail"]


class ReminderResult(BookingModel):
    attempt_id: AttemptId = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    outcome: ReminderOutcome
    # Намерение пациента и подтверждённая запись в МИС — разные факты.
    mis_status: Literal["not_required", "applied", "pending", "manual"] = "not_required"

    @model_validator(mode="after")
    def write_outcome_has_a_status(self) -> ReminderResult:
        if self.outcome in {"CONFIRMED", "RESCHEDULED", "CANCELLED", "OPT_OUT"}:
            if self.mis_status == "not_required":
                raise ValueError("для изменения МИС нужен явный статус фиксации")
        return self


class OutboundDialogue(Protocol):
    async def open_outbound(self, call_id: str, context: OutboundContext) -> Reply:
        """Первый ответ — turn_id=1; первая речь пациента получает номер 2."""
        ...

    async def outbound_event(self, call_id: str, event: OutboundEvent) -> Reply: ...
