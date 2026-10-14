"""Внутренние модели backend; это не согласованные контракты NLU."""

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.mis.models import MisId, parse_datetime


class BookingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Slot(BookingModel):
    doctor_id: MisId
    scheduled_at: AwareDatetime
    specialty: str = Field(min_length=1, max_length=100)

    @field_validator("scheduled_at")
    @classmethod
    def minute_precision(cls, value: datetime) -> datetime:
        return parse_datetime(value)


class ActionState(StrEnum):
    HELD = "held"
    COMMITTING = "committing"
    RECONCILING = "reconciling"
    BOOKED = "booked"
    ARCHIVED = "archived"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


ACTIVE_STATES = {
    ActionState.HELD,
    ActionState.COMMITTING,
    ActionState.RECONCILING,
    ActionState.BOOKED,
}


class BookingAction(BookingModel):
    action_id: str
    call_id: str
    revision: int
    client_id: MisId = Field(repr=False)
    slot: Slot
    expires_at: AwareDatetime
    state: ActionState = ActionState.HELD
    confirmed_turn: int | None = None
    visit_id: int | None = None


class Approval(BookingModel):
    call_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    action_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(strict=True, ge=1)
    turn_id: int = Field(strict=True, ge=1)
    is_final: StrictBool = False
    permitted: StrictBool = False


class BookingError(Exception):
    """Машиночитаемая причина остановки без данных пациента."""


def check_approval(action: BookingAction, approval: Approval) -> None:
    if (
        (approval.call_id, approval.action_id, approval.revision)
        != (action.call_id, action.action_id, action.revision)
        or not approval.is_final
        or not approval.permitted
    ):
        raise BookingError("CONFIRMATION_REQUIRED")
    if action.confirmed_turn is not None and action.confirmed_turn != approval.turn_id:
        raise BookingError("STALE_CONFIRMATION")
