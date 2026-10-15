"""Связываем подтверждение отмены с показанной пациенту записью и её журналом."""

from pydantic import ValidationError

from app.booking.guard import check_dialogue_guard
from app.booking.models import Approval, BookingError
from app.cancellation.models import CancelAction, CancelProposal, summary


def check_cancel_guard(state: dict | None, approval: Approval, action: CancelAction) -> None:
    check_dialogue_guard(state, approval, phase="cancel_confirm", proposal_field="cancel_proposal")
    try:
        proposal = CancelProposal.model_validate(state["cancel_proposal"])
    except (ValidationError, KeyError, TypeError):
        raise BookingError("CONFIRMATION_INTERRUPTED") from None
    if (
        proposal.visit != summary(action.before)
        or state.get("client_id") != action.before.client_id
    ):
        raise BookingError("CONFIRMATION_INTERRUPTED")
