"""Автономный журнал отмены, атомарный вместе с местным журналом создания."""

from datetime import datetime, timedelta
from uuid import uuid4

from app.booking.models import ActionState, Approval, BookingError
from app.cancellation.models import CancelAction, CancelState, Reason, check_approval
from app.mis.models import Visit
from app.outbox.models import cancellation_events
from app.rescheduling.models import active as move_active
from app.storage.memory_ledger import MemoryLedger

ACTIVE = {CancelState.PREPARED, CancelState.COMMITTING, CancelState.RECONCILING}


class MemoryCancellationStore:
    def __init__(self, ledger: MemoryLedger, guard_reader=None) -> None:
        self.ledger, self.guard_reader = ledger, guard_reader

    async def prepare(
        self, call_id: str, before: Visit, now: datetime, reason: Reason | None = None
    ) -> CancelAction:
        async with self.ledger.lock:
            if any(
                move_active(m, now)
                and (
                    m.before.id == before.id
                    or m.new_visit is not None
                    and m.new_visit.id == before.id
                )
                for m in self.ledger.moves.values()
            ):
                raise BookingError("ACTION_PENDING")
            for key, action in self.ledger.cancellations.items():
                if action.state == CancelState.PREPARED and action.expires_at <= now:
                    self.ledger.cancellations[key] = action.model_copy(
                        update={"state": CancelState.EXPIRED}
                    )
            previous = [a for a in self.ledger.cancellations.values() if a.call_id == call_id]
            if any(a.state in {CancelState.COMMITTING, CancelState.RECONCILING} for a in previous):
                raise BookingError("ACTION_PENDING")
            if any(
                a.state in {ActionState.COMMITTING, ActionState.RECONCILING}
                and a.client_id == before.client_id
                and a.slot.doctor_id == before.doctor.id
                and a.slot.scheduled_at == before.scheduled_at
                for a in self.ledger.bookings.values()
            ):
                raise BookingError("ACTION_PENDING")
            if any(
                a.before.id == before.id and a.call_id != call_id and a.state in ACTIVE
                for a in self.ledger.cancellations.values()
            ):
                raise BookingError("CANCELLATION_BUSY")
            for action in previous:
                if action.state == CancelState.PREPARED:
                    self.ledger.cancellations[action.action_id] = action.model_copy(
                        update={"state": CancelState.ABORTED}
                    )
            action = CancelAction(
                action_id=uuid4().hex,
                call_id=call_id,
                before=before,
                reason=reason,
                revision=max((a.revision for a in previous), default=0) + 1,
                expires_at=now + timedelta(seconds=90),
            )
            self.ledger.cancellations[action.action_id] = action
            return action

    async def get(self, action_id: str) -> CancelAction:
        async with self.ledger.lock:
            if action_id not in self.ledger.cancellations:
                raise BookingError("ACTION_NOT_FOUND")
            return self.ledger.cancellations[action_id]

    async def claim(self, approval: Approval, now: datetime) -> tuple[CancelAction, bool]:
        async with self.ledger.lock:
            action = self.ledger.cancellations.get(approval.action_id)
            if action is None:
                raise BookingError("ACTION_NOT_FOUND")
            check_approval(action, approval)
            if action.state in {
                CancelState.COMMITTING,
                CancelState.RECONCILING,
                CancelState.CANCELLED,
            }:
                return action, False
            if action.state != CancelState.PREPARED or action.expires_at <= now:
                raise BookingError("HOLD_NOT_ACTIVE")
            if action.before.scheduled_at <= now:
                raise BookingError("VISIT_PAST")
            if approval.guard_version is not None:
                if self.guard_reader is None:
                    raise BookingError("DIALOGUE_GUARD_REQUIRED")
                await self.guard_reader.check_cancel_guard(approval, action)
            action = action.model_copy(
                update={
                    "state": CancelState.COMMITTING,
                    "confirmed_turn": approval.turn_id,
                    "late": action.before.scheduled_at < now + timedelta(hours=2),
                }
            )
            self.ledger.cancellations[action.action_id] = action
            return action, True

    async def finish(self, action_id: str, state: CancelState, now: datetime) -> CancelAction:
        async with self.ledger.lock:
            action = self.ledger.cancellations[action_id]
            if action.state not in {CancelState.COMMITTING, CancelState.RECONCILING}:
                if action.state == state:
                    return action
                raise BookingError("INVALID_TRANSITION")
            if state not in {CancelState.CANCELLED, CancelState.RECONCILING, CancelState.REJECTED}:
                raise BookingError("INVALID_TRANSITION")
            if state == CancelState.CANCELLED:
                # Готовим оба события до изменения журнала: ошибка не даёт половинчатого результата.
                events = cancellation_events(action, now)
                for key, booking in self.ledger.bookings.items():
                    if (
                        booking.state == ActionState.BOOKED
                        and booking.visit_id == action.before.id
                        and booking.client_id == action.before.client_id
                        and booking.slot.doctor_id == action.before.doctor.id
                        and booking.slot.scheduled_at == action.before.scheduled_at
                        and booking.slot.specialty == action.before.doctor.specialty
                    ):
                        self.ledger.bookings[key] = booking.model_copy(
                            update={"state": ActionState.ARCHIVED}
                        )
                for event in events:
                    self.ledger.outbox.setdefault(event.event_id, event)
            action = action.model_copy(
                update={
                    "state": state,
                    "completed_at": now if state == CancelState.CANCELLED else None,
                }
            )
            self.ledger.cancellations[action_id] = action
            return action

    async def restore_unsent(self, action_id: str, turn_id: int) -> CancelAction:
        async with self.ledger.lock:
            action = self.ledger.cancellations[action_id]
            if action.state != CancelState.COMMITTING or action.confirmed_turn != turn_id:
                raise BookingError("ACTION_NOT_UNSENT")
            action = action.model_copy(
                update={"state": CancelState.PREPARED, "confirmed_turn": None}
            )
            self.ledger.cancellations[action_id] = action
            return action

    async def release_call(self, call_id: str) -> None:
        async with self.ledger.lock:
            for key, action in self.ledger.cancellations.items():
                if action.call_id == call_id and action.state == CancelState.PREPARED:
                    self.ledger.cancellations[key] = action.model_copy(
                        update={"state": CancelState.ABORTED}
                    )
