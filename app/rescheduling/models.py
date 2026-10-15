"""Сохраняемый журнал двух изменений МИС и предложение с обеими целями."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime

from app.booking.models import Approval, BookingError, BookingModel, Slot
from app.cancellation.models import summary
from app.mis.models import Visit, VisitStatus
from app.visits.models import VisitSummary


class MoveState(StrEnum):
    HELD = "held"
    CREATING = "creating"
    CREATE_UNKNOWN = "create_unknown"
    NEW_CREATED = "new_created"
    CANCELLING = "cancelling"
    CANCEL_UNKNOWN = "cancel_unknown"
    RESCHEDULED = "rescheduled"
    MANUAL = "manual"
    REJECTED = "rejected"
    ABORTED = "aborted"
    EXPIRED = "expired"


ACTIVE_MOVES = {
    MoveState.HELD,
    MoveState.CREATING,
    MoveState.CREATE_UNKNOWN,
    MoveState.NEW_CREATED,
    MoveState.CANCELLING,
    MoveState.CANCEL_UNKNOWN,
    MoveState.MANUAL,
}


class MoveAction(BookingModel):
    action_id: str
    call_id: str
    revision: int
    before: Visit
    slot: Slot
    expires_at: AwareDatetime
    state: MoveState = MoveState.HELD
    create_turn: int | None = None
    cancel_turn: int | None = None
    new_visit: Visit | None = None
    cancellation_action_id: str | None = None
    room: str | None = None
    doctor_name: str | None = None


class MoveProposal(BookingModel):
    action_id: str
    revision: int
    before: VisitSummary
    slot: Slot
    expires_at: AwareDatetime
    stage: Literal["move", "finish"] = "move"
    new_visit_id: int | None = None
    room: str | None = None
    doctor_name: str | None = None


def proposal(action: MoveAction, *, stage: Literal["move", "finish"] = "move") -> MoveProposal:
    return MoveProposal(
        action_id=action.action_id,
        revision=action.revision,
        before=summary(action.before),
        slot=action.slot,
        expires_at=action.expires_at,
        stage=stage,
        new_visit_id=action.new_visit.id if stage == "finish" and action.new_visit else None,
        room=action.room,
        doctor_name=action.doctor_name,
    )


def active(action: MoveAction, now: datetime) -> bool:
    return action.state in ACTIVE_MOVES and not (
        action.state == MoveState.HELD and action.expires_at <= now
    )


def matches_new(action: MoveAction, visit: Visit) -> bool:
    return (
        visit.id != action.before.id
        and visit.client_id == action.before.client_id
        and visit.doctor.id == action.slot.doctor_id
        and visit.doctor.specialty == action.slot.specialty
        and visit.scheduled_at == action.slot.scheduled_at
        and visit.status == VisitStatus.PLANNED
    )


def check_approval(action: MoveAction, approval: Approval) -> None:
    if (
        (action.call_id, action.action_id, action.revision)
        != (approval.call_id, approval.action_id, approval.revision)
        or not approval.is_final
        or not approval.permitted
    ):
        raise BookingError("CONFIRMATION_REQUIRED")
