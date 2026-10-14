"""Перенос, аудит отмены, новая местная запись и outbox под единым замком."""

from contextlib import asynccontextmanager
from datetime import timedelta

from app.booking.models import ActionState, BookingAction, BookingError
from app.cancellation.models import CancelAction, CancelState
from app.outbox.models import cancellation_events
from app.rescheduling.journal import MoveJournal
from app.rescheduling.models import MoveState
from app.storage.memory_ledger import MemoryLedger


class MemoryMoveStore(MoveJournal):
    def __init__(self, ledger: MemoryLedger, guard_reader=None):
        self.ledger, self.guard_reader = ledger, guard_reader

    @asynccontextmanager
    async def _transaction(self):
        async with self.ledger.lock:
            yield self.ledger

    async def _read(self, tx, action_id):
        if action_id not in tx.moves:
            raise BookingError("ACTION_NOT_FOUND")
        return tx.moves[action_id]

    async def _write(self, tx, action):
        tx.moves[action.action_id] = action
        return action

    async def _moves(self, tx, call_id):
        return tuple(tx.moves.values())

    async def _bookings(self, tx):
        return tuple(tx.bookings.values())

    async def _cancellations(self, tx):
        return tuple(tx.cancellations.values())

    async def _guard(self, tx, approval, action):
        if self.guard_reader is None:
            raise BookingError("DIALOGUE_GUARD_REQUIRED")
        await self.guard_reader.check_move_guard(approval, action)

    async def _complete(self, tx, action, now):
        before, new = action.before, action.new_visit
        audit = CancelAction(
            action_id=action.action_id,
            call_id=action.call_id,
            revision=max(
                (c.revision for c in tx.cancellations.values() if c.call_id == action.call_id),
                default=0,
            )
            + 1,
            before=before,
            expires_at=action.expires_at,
            state=CancelState.CANCELLED,
            confirmed_turn=action.cancel_turn,
            reason="reschedule",
            late=before.scheduled_at < now + timedelta(hours=2),
            completed_at=now,
        )
        events = cancellation_events(audit, now, new_visit=new, room=action.room)
        booked = BookingAction(
            action_id=action.action_id,
            call_id=action.call_id,
            revision=max(
                (b.revision for b in tx.bookings.values() if b.call_id == action.call_id), default=0
            )
            + 1,
            client_id=before.client_id,
            slot=action.slot,
            expires_at=action.expires_at,
            state=ActionState.BOOKED,
            confirmed_turn=action.create_turn,
            visit_id=new.id,
        )
        for key, b in tx.bookings.items():
            if b.state == ActionState.BOOKED and (
                b.visit_id == before.id
                and b.client_id == before.client_id
                and b.slot.doctor_id == before.doctor.id
                and b.slot.scheduled_at == before.scheduled_at
                and b.slot.specialty == before.doctor.specialty
                or b.slot.scheduled_at < now
            ):
                tx.bookings[key] = b.model_copy(update={"state": ActionState.ARCHIVED})
            elif b.state == ActionState.HELD and b.expires_at <= now:
                tx.bookings[key] = b.model_copy(update={"state": ActionState.EXPIRED})
        tx.bookings[booked.action_id] = booked
        tx.cancellations[audit.action_id] = audit
        for event in events:
            tx.outbox.setdefault(event.event_id, event)
        return await self._write(
            tx,
            action.model_copy(
                update={"state": MoveState.RESCHEDULED, "cancellation_action_id": audit.action_id}
            ),
        )
