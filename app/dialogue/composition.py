"""Общая сборка диалога: внешние ресурсы выбираются до создания сценариев."""

from app.booking.service import BookingService
from app.cancellation.service import CancellationService
from app.dialogue.identity import IdentityDrafts
from app.dialogue.natural import BookingConversation
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.rescheduling.service import RescheduleService
from app.scheduling.index import SqliteSchedule


def assemble_dialogue(
    settings,
    clock,
    mis,
    schedule,
    booking_store,
    dialogue_store,
    cancellation_store,
    move_store,
    hash_key,
):
    booking = BookingService(
        mis,
        booking_store,
        schedule,
        clock,
        require_dialogue_guard=settings.dialogue_mode == "natural",
    )
    safety = RulesSafety(clock) if settings.dialogue_mode == "natural" else None
    conversation = (
        BookingConversation(
            booking,
            dialogue_store,
            safety,
            rooms=schedule if isinstance(schedule, SqliteSchedule) else None,
            cancellation=CancellationService(mis, cancellation_store, clock),
            rescheduling=RescheduleService(booking, move_store),
            identity_drafts=IdentityDrafts(hash_key),
        )
        if safety is not None
        else None
    )
    return DialogueService(
        booking,
        dialogue_store,
        hash_key,
        safety=safety,
        conversation=conversation,
        mis_turn_wait_s=settings.mis_turn_wait_s,
        mis_wait_limit_s=settings.mis_wait_limit_s,
    )
