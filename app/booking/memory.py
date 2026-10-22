"""Хранилище автономного стенда. Внешнюю МИС с ним подключать нельзя."""

from datetime import datetime, timedelta
from uuid import uuid4

from app.booking.models import (
    ACTIVE_STATES,
    ActionState,
    Approval,
    BookingAction,
    BookingError,
    Slot,
    check_approval,
)
from app.rescheduling.models import ACTIVE_MOVES, MoveState
from app.rescheduling.models import active as move_active
from app.storage.memory_ledger import MemoryLedger


class MemoryBookingStore:
    def __init__(self, guard_reader=None) -> None:
        self.ledger = MemoryLedger()
        self.actions = self.ledger.bookings
        self._lock = self.ledger.lock
        self._guard_reader = guard_reader

    def _expire(self, now: datetime) -> None:
        for key, move in self.ledger.moves.items():
            if move.state == "held" and move.expires_at <= now:
                self.ledger.moves[key] = move.model_copy(update={"state": MoveState.EXPIRED})
        for key, action in self.actions.items():
            if action.state == ActionState.HELD and action.expires_at <= now:
                self.actions[key] = action.model_copy(update={"state": ActionState.EXPIRED})
            elif action.state == ActionState.BOOKED and action.slot.scheduled_at < now:
                self.actions[key] = action.model_copy(update={"state": ActionState.ARCHIVED})

    def _available(self, slot: Slot) -> bool:
        return not any(
            m.state in ACTIVE_MOVES
            and m.slot.doctor_id == slot.doctor_id
            and m.slot.scheduled_at == slot.scheduled_at
            for m in self.ledger.moves.values()
        ) and not any(
            a.slot.doctor_id == slot.doctor_id
            and a.slot.scheduled_at == slot.scheduled_at
            and a.state in ACTIVE_STATES
            for a in self.actions.values()
        )

    async def available(self, slot: Slot, now: datetime) -> bool:
        async with self._lock:
            self._expire(now)
            return self._available(slot)

    async def hold(self, call_id: str, client_id: int, slot: Slot, now: datetime) -> BookingAction:
        async with self._lock:
            self._expire(now)
            previous = [a for a in self.actions.values() if a.call_id == call_id]
            if any(
                move_active(m, now)
                and (
                    (m.slot.doctor_id, m.slot.scheduled_at) == (slot.doctor_id, slot.scheduled_at)
                    or m.before.client_id == client_id
                    and m.slot.specialty == slot.specialty
                )
                for m in self.ledger.moves.values()
            ):
                raise BookingError("ACTION_PENDING")
            if any(a.state in {ActionState.COMMITTING, ActionState.RECONCILING} for a in previous):
                raise BookingError("ACTION_PENDING")
            # Изменение выбора аннулирует предыдущую бронь и её подтверждение.
            occupied = [
                a
                for a in self.actions.values()
                if a.state in ACTIVE_STATES
                and not (a.call_id == call_id and a.state == ActionState.HELD)
            ]
            if any(
                a.slot.doctor_id == slot.doctor_id and a.slot.scheduled_at == slot.scheduled_at
                for a in occupied
            ):
                raise BookingError("SLOT_BUSY")
            if any(
                a.client_id == client_id and a.slot.specialty == slot.specialty for a in occupied
            ):
                raise BookingError("SPECIALTY_ALREADY_BOOKED")
            for action in previous:
                if action.state == ActionState.HELD:
                    self.actions[action.action_id] = action.model_copy(
                        update={"state": ActionState.CANCELLED}
                    )
            action = BookingAction(
                action_id=uuid4().hex,
                call_id=call_id,
                revision=max((a.revision for a in previous), default=0) + 1,
                client_id=client_id,
                slot=slot,
                expires_at=now + timedelta(seconds=90),
            )
            self.actions[action.action_id] = action
            return action

    async def get(self, action_id: str) -> BookingAction:
        async with self._lock:
            if action_id not in self.actions:
                raise BookingError("ACTION_NOT_FOUND")
            return self.actions[action_id]

    async def archive_external(self, action_id: str, visit_id: int) -> BookingAction:
        async with self._lock:
            action = self.actions[action_id]
            if (
                action.state not in {ActionState.BOOKED, ActionState.ARCHIVED}
                or action.visit_id != visit_id
            ):
                raise BookingError("VISIT_NOT_CONFIRMED")
            action = action.model_copy(update={"state": ActionState.ARCHIVED})
            self.actions[action_id] = action
            return action

    async def claim(self, approval: Approval, now: datetime) -> tuple[BookingAction, bool]:
        async with self._lock:
            self._expire(now)
            action = self.actions.get(approval.action_id)
            if action is None:
                raise BookingError("ACTION_NOT_FOUND")
            check_approval(action, approval)
            if approval.guard_version is not None:
                if self._guard_reader is None:
                    raise BookingError("DIALOGUE_GUARD_REQUIRED")
                await self._guard_reader.check_guard(approval)
            if action.state in {
                ActionState.ARCHIVED,
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }:
                return action, False
            if action.state != ActionState.HELD:
                raise BookingError("HOLD_NOT_ACTIVE")
            action = action.model_copy(
                update={"state": ActionState.COMMITTING, "confirmed_turn": approval.turn_id}
            )
            self.actions[action.action_id] = action
            return action, True

    async def finish(
        self,
        action_id: str,
        state: ActionState,
        visit_id: int | None = None,
        now: datetime | None = None,
    ) -> BookingAction:
        async with self._lock:
            action = self.actions[action_id]
            if action.state not in {ActionState.COMMITTING, ActionState.RECONCILING}:
                if action.state == state and action.visit_id == visit_id:
                    return action
                raise BookingError("INVALID_TRANSITION")
            if state not in {ActionState.BOOKED, ActionState.REJECTED, ActionState.RECONCILING}:
                raise BookingError("INVALID_TRANSITION")
            if (state == ActionState.BOOKED) != (visit_id is not None):
                raise BookingError("VISIT_ID_REQUIRED")
            action = action.model_copy(update={"state": state, "visit_id": visit_id})
            self.actions[action_id] = action
            return action

    async def cancel(self, action_id: str, call_id: str) -> BookingAction:
        async with self._lock:
            action = self.actions.get(action_id)
            if action is None or action.call_id != call_id:
                raise BookingError("ACTION_NOT_FOUND")
            if action.state != ActionState.HELD:
                raise BookingError("HOLD_NOT_ACTIVE")
            action = action.model_copy(update={"state": ActionState.CANCELLED})
            self.actions[action_id] = action
            return action

    async def restore_unsent(self, action_id: str, confirmed_turn: int) -> BookingAction:
        async with self._lock:
            action = self.actions.get(action_id)
            if (
                action is None
                or action.state != ActionState.COMMITTING
                or action.confirmed_turn != confirmed_turn
            ):
                raise BookingError("ACTION_NOT_UNSENT")
            # Срок первоначальной брони не продлеваем ожиданием бюджета.
            action = action.model_copy(update={"state": ActionState.HELD, "confirmed_turn": None})
            self.actions[action_id] = action
            return action

    async def release_call_holds(self, call_id: str) -> None:
        async with self._lock:
            for key, action in self.actions.items():
                if action.call_id == call_id and action.state == ActionState.HELD:
                    self.actions[key] = action.model_copy(update={"state": ActionState.CANCELLED})
