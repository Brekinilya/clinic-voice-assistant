"""Оба назначения и пациент связаны с сохранённым разрешением разговора."""

from app.booking.guard import check_dialogue_guard
from app.booking.models import Approval, BookingError
from app.rescheduling.models import MoveAction, MoveProposal, proposal


def check_move_guard(state: dict | None, approval: Approval, action: MoveAction) -> None:
    if state is None or state.get("phase") not in {"move_confirm", "move_finish_confirm"}:
        raise BookingError("CONFIRMATION_INTERRUPTED")
    check_dialogue_guard(state, approval, phase=state["phase"], proposal_field="move_proposal")
    shown = MoveProposal.model_validate(state["move_proposal"])
    if (
        shown != proposal(action, stage=shown.stage)
        or state.get("client_id") != action.before.client_id
    ):
        raise BookingError("CONFIRMATION_INTERRUPTED")
    if shown.stage == "finish" and state["phase"] != "move_finish_confirm":
        raise BookingError("CONFIRMATION_INTERRUPTED")
    if (
        shown.stage == "move"
        and action.create_turn is not None
        and approval.turn_id != action.create_turn
    ):
        raise BookingError("STALE_CONFIRMATION")
