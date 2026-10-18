"""Внутренний контракт T11. Контекст задания не заменяет согласие на действие."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from app.booking.models import BookingModel
from app.mis.models import MisId
from app.visits.models import VisitSummary

if TYPE_CHECKING:
    from app.booking.models import Approval
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


class ReminderBinding(BookingModel):
    """Область полномочий попытки; имени пациента в сохраняемом состоянии нет."""

    attempt_id: AttemptId = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    fence: int = Field(strict=True, ge=1)
    purpose: Literal["reminder"] = "reminder"
    client_id: MisId = Field(repr=False)
    visit: VisitSummary


class OutboundContext(ReminderBinding):
    """Только из доверенного исполнителя T11, не из текста/публичного HTTP."""

    # Только имя и отчество из проверенной карточки; не сериализовать в диагностику.
    addressee: str = Field(min_length=1, max_length=201, repr=False)

    @field_validator("addressee")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("нужно имя адресата без управляющих символов")
        return value.strip()


class ReminderSession(BookingModel):
    binding: ReminderBinding
    context_digest: str = Field(repr=False)
    verified: bool = False
    declined: bool = False


MarkStatus = Literal["applied", "pending", "manual"]
MarkOperation = Literal["confirm", "opt_out"]


def mark_action_id(binding: ReminderBinding, operation: MarkOperation) -> str:
    # Длинный attempt_id не превышает лимит Approval.action_id; ключ не содержит ПДн.
    digest = hashlib.sha256(binding.attempt_id.encode()).hexdigest()
    return f"reminder:{digest}:{operation}"


class ReminderActions(Protocol):
    """Адаптер Ильи: актуальная lease/fence и долговечный журнал отметок.

    Перед PATCH адаптер повторно проверяет попытку, check_reminder_guard и свежий
    визит/комментарий. Повтор (attempt_id, operation) читает журнал, не повторяет PATCH.
    pending/manual возвращаются после сохранения результата; неизвестный исход не
    превращается в исключение, которое могло бы разрешить повторную отправку.
    """

    async def check_attempt(self, binding: ReminderBinding) -> None: ...
    async def stop_calls(self, binding: ReminderBinding) -> None:
        """Устойчивый запрет звонить на связанный контакт, даже до проверки адресата.

        Только локальная доставка: комментарий пациента без проверки личности не меняется.
        Повтор идемпотентен; не относится к попыткам изменения визита.
        """
        ...

    async def confirm(self, binding: ReminderBinding, approval: Approval) -> MarkStatus: ...
    async def opt_out(self, binding: ReminderBinding, approval: Approval) -> MarkStatus: ...


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
