"""Общая сборка диалога: внешние ресурсы выбираются до создания сценариев."""

from app.booking.service import BookingService
from app.cancellation.service import CancellationService
from app.dialogue.identity import IdentityDrafts
from app.dialogue.natural import BookingConversation
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.service import HandoffService, HandoffSettings
from app.recovery.coordinator import OperationCoordinator
from app.recovery.memory import MemoryRecoveryStore
from app.rescheduling.service import RescheduleService
from app.scheduling.index import SqliteSchedule
from app.storage.booking import PostgresBookingStore
from app.storage.handoffs import PostgresHandoffStore
from app.storage.recovery import PostgresRecoveryStore


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
    *,
    reminder_actions=None,
):
    recovery_store = (
        PostgresRecoveryStore(booking_store.engine)
        if isinstance(booking_store, PostgresBookingStore)
        else MemoryRecoveryStore(booking_store.ledger)
    )
    operations = OperationCoordinator(recovery_store, clock)
    booking = BookingService(
        mis,
        booking_store,
        schedule,
        clock,
        require_dialogue_guard=settings.dialogue_mode == "natural",
        operations=operations,
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
    handoffs = None
    if settings.handoffs_enabled:
        handoff_store = PostgresHandoffStore(booking_store.engine)
        handoffs = HandoffService(
            handoff_store,
            mis,
            clock,
            hash_key,
            HandoffSettings(
                enabled=True,
                callback_enabled=settings.callback_enabled,
                callback_endpoint=settings.callback_endpoint,
                technical_client_id=settings.handoff_technical_client_id,
            ),
        )
    return DialogueService(
        booking,
        dialogue_store,
        hash_key,
        safety=safety,
        conversation=conversation,
        mis_turn_wait_s=settings.mis_turn_wait_s,
        mis_wait_limit_s=settings.mis_wait_limit_s,
        reminder_actions=reminder_actions,
        handoffs=handoffs,
    )
