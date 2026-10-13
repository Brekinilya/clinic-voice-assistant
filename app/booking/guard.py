"""Сохранённое серверное разрешение сверяется атомарно с началом операции."""

from app.booking.models import Approval, BookingError


def check_dialogue_guard(state: dict | None, approval: Approval) -> None:
    if state is None:
        raise BookingError("CONFIRMATION_INTERRUPTED")
    authorization = state.get("authorization") or {}
    proposal = state.get("proposal") or {}
    if (
        state.get("phase") != "held"
        or state.get("version") != approval.guard_version
        or state.get("input_epoch") != approval.input_epoch
        or state.get("observed_turn_id") != approval.turn_id
        or state.get("pending_turn_id") != approval.turn_id
        or authorization
        != {
            "turn_id": approval.turn_id,
            "version": approval.guard_version,
            "input_epoch": approval.input_epoch,
            "action_id": approval.action_id,
            "revision": approval.revision,
        }
        or proposal.get("action_id") != approval.action_id
        or proposal.get("revision") != approval.revision
    ):
        raise BookingError("CONFIRMATION_INTERRUPTED")
