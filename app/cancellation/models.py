"""Неизменяемый снимок выбранной записи и состояние операции отмены."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime

from app.booking.models import Approval, BookingError, BookingModel
from app.mis.models import Visit
from app.visits.models import VisitSummary

Reason = Literal["not_needed", "cannot_attend", "reschedule"]


class CancelState(StrEnum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    RECONCILING = "reconciling"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ABORTED = "aborted"
    EXPIRED = "expired"


class CancelAction(BookingModel):
    action_id: str
    call_id: str
    revision: int
    before: Visit
    expires_at: AwareDatetime
    state: CancelState = CancelState.PREPARED
    confirmed_turn: int | None = None
    reason: Reason | None = None
    late: bool = False
    completed_at: AwareDatetime | None = None


class CancelProposal(BookingModel):
    action_id: str
    revision: int
    visit: VisitSummary
    expires_at: AwareDatetime


def summary(visit: Visit) -> VisitSummary:
    return VisitSummary(
        visit_id=visit.id,
        doctor_id=visit.doctor.id,
        doctor_name=visit.doctor.full_name,
        specialty=visit.doctor.specialty,
        scheduled_at=visit.scheduled_at,
    )


def same_visit(before: Visit, current: Visit) -> bool:
    return (
        before.id == current.id
        and before.client_id == current.client_id
        and before.doctor.id == current.doctor.id
        and before.doctor.specialty == current.doctor.specialty
        and before.scheduled_at == current.scheduled_at
        and before.created_at == current.created_at
    )


def check_approval(action: CancelAction, approval: Approval) -> None:
    if (
        (action.call_id, action.action_id, action.revision)
        != (approval.call_id, approval.action_id, approval.revision)
        or not approval.is_final
        or not approval.permitted
    ):
        raise BookingError("CONFIRMATION_REQUIRED")
    if action.confirmed_turn is not None and action.confirmed_turn != approval.turn_id:
        raise BookingError("STALE_CONFIRMATION")
