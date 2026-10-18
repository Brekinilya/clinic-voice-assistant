"""Разрешение отметки С-10 отзывается новой речью, отбоем и сменой попытки."""

from app.booking.models import Approval, BookingError
from app.dialogue.outbound import MarkOperation, ReminderBinding, mark_action_id


def check_reminder_guard(
    state: dict | None,
    approval: Approval,
    binding: ReminderBinding,
    operation: MarkOperation,
) -> None:
    state = state or {}
    session = state.get("reminder") or {}
    expected = {
        "turn_id": approval.turn_id,
        "version": approval.guard_version,
        "input_epoch": approval.input_epoch,
        "action_id": mark_action_id(binding, operation),
        "revision": binding.fence,
    }
    if (
        not approval.is_final
        or not approval.permitted
        or state.get("phase") in {"ended", "handoff", "interrupted"}
        or state.get("call_id") != approval.call_id
        or state.get("client_id") != binding.client_id
        or state.get("version") != approval.guard_version
        or state.get("input_epoch") != approval.input_epoch
        or state.get("observed_turn_id") != approval.turn_id
        or state.get("pending_turn_id") != approval.turn_id
        or not session.get("verified")
        or session.get("binding") != binding.model_dump(mode="json")
        or state.get("authorization") != expected
        or approval.action_id != expected["action_id"]
        or approval.revision != binding.fence
        or operation == "confirm"
        and (state.get("phase") != "remind_details" or session.get("declined"))
    ):
        raise BookingError("CONFIRMATION_INTERRUPTED")
