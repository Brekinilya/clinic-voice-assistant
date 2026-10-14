"""Общие переходы журналов. Реализации задают замок, чтение, запись и атомарный итог."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from uuid import uuid4

from app.booking.models import ACTIVE_STATES, ActionState, Approval, BookingError, Slot
from app.cancellation.models import CancelState, same_visit
from app.mis.models import Visit, VisitStatus
from app.rescheduling.models import MoveAction, MoveState, active, check_approval, matches_new


class MoveJournal(ABC):
    @abstractmethod
    def _transaction(self): ...

    @abstractmethod
    async def _read(self, tx, action_id) -> MoveAction: ...

    @abstractmethod
    async def _write(self, tx, action) -> MoveAction: ...

    @abstractmethod
    async def _moves(self, tx, call_id): ...

    @abstractmethod
    async def _bookings(self, tx): ...

    @abstractmethod
    async def _cancellations(self, tx): ...

    @abstractmethod
    async def _guard(self, tx, approval, action): ...

    @abstractmethod
    async def _complete(self, tx, action, now) -> MoveAction: ...

    async def get(self, action_id: str) -> MoveAction:
        async with self._transaction() as tx:
            return await self._read(tx, action_id)

    async def prepare(
        self, call_id: str, before: Visit, slot: Slot, now: datetime, room=None, doctor_name=None
    ):
        if before.status != VisitStatus.PLANNED or before.scheduled_at <= now:
            raise BookingError("VISIT_PAST")
        if slot.specialty != before.doctor.specialty or (slot.doctor_id, slot.scheduled_at) == (
            before.doctor.id,
            before.scheduled_at,
        ):
            raise BookingError("WRONG_SPECIALTY")
        async with self._transaction() as tx:
            moves = await self._moves(tx, call_id)
            for move in moves:
                if move.state == MoveState.HELD and move.expires_at <= now:
                    await self._write(tx, move.model_copy(update={"state": MoveState.EXPIRED}))
            previous = [m for m in moves if m.call_id == call_id]
            if any(active(m, now) and m.state != MoveState.HELD for m in previous):
                raise BookingError("ACTION_PENDING")
            for m in moves:
                if not active(m, now) or (m.call_id == call_id and m.state == MoveState.HELD):
                    continue
                if (
                    m.before.id == before.id
                    or m.before.client_id == before.client_id
                    and m.slot.specialty == slot.specialty
                ):
                    raise BookingError("ACTION_PENDING")
                if (m.slot.doctor_id, m.slot.scheduled_at) == (slot.doctor_id, slot.scheduled_at):
                    raise BookingError("SLOT_BUSY")
            for cancellation in await self._cancellations(tx):
                if (
                    cancellation.before.id == before.id
                    and cancellation.state
                    in {CancelState.PREPARED, CancelState.COMMITTING, CancelState.RECONCILING}
                    and not (
                        cancellation.state == CancelState.PREPARED
                        and cancellation.expires_at <= now
                    )
                ):
                    raise BookingError("CANCELLATION_BUSY")
            for booking in await self._bookings(tx):
                if (
                    booking.state not in ACTIVE_STATES
                    or booking.state == ActionState.HELD
                    and booking.expires_at <= now
                    or booking.state == ActionState.BOOKED
                    and booking.slot.scheduled_at < now
                ):
                    continue
                old_known = (
                    booking.state == ActionState.BOOKED
                    and booking.visit_id == before.id
                    and booking.client_id == before.client_id
                    and booking.slot.doctor_id == before.doctor.id
                    and booking.slot.scheduled_at == before.scheduled_at
                )
                if old_known:
                    continue
                if (
                    booking.client_id == before.client_id
                    and booking.slot.specialty == slot.specialty
                ):
                    raise BookingError("ACTION_PENDING")
                if (booking.slot.doctor_id, booking.slot.scheduled_at) == (
                    slot.doctor_id,
                    slot.scheduled_at,
                ):
                    raise BookingError("SLOT_BUSY")
            for m in previous:
                if m.state == MoveState.HELD:
                    await self._write(tx, m.model_copy(update={"state": MoveState.ABORTED}))
            action = MoveAction(
                action_id=uuid4().hex,
                call_id=call_id,
                revision=max((m.revision for m in previous), default=0) + 1,
                before=before,
                slot=slot,
                expires_at=now + timedelta(seconds=90),
                room=room,
                doctor_name=doctor_name,
            )
            return await self._write(tx, action)

    async def claim(self, approval: Approval, now: datetime, *, cancelling=False):
        async with self._transaction() as tx:
            action = await self._read(tx, approval.action_id)
            check_approval(action, approval)
            expected = MoveState.NEW_CREATED if cancelling else MoveState.HELD
            if action.state != expected:
                return action, False
            if action.expires_at <= now or action.before.scheduled_at <= now:
                raise BookingError("HOLD_NOT_ACTIVE")
            if approval.guard_version is not None:
                await self._guard(tx, approval, action)
            action = action.model_copy(
                update={
                    "state": MoveState.CANCELLING if cancelling else MoveState.CREATING,
                    "cancel_turn" if cancelling else "create_turn": approval.turn_id,
                }
            )
            return await self._write(tx, action), True

    async def created(self, action_id: str, visit: Visit) -> MoveAction:
        async with self._transaction() as tx:
            action = await self._read(tx, action_id)
            if not matches_new(action, visit):
                raise BookingError("NEW_VISIT_NOT_CONFIRMED")
            if action.new_visit is not None:
                if action.new_visit.id == visit.id:
                    return action
                return await self._write(tx, action.model_copy(update={"state": MoveState.MANUAL}))
            if action.state not in {MoveState.CREATING, MoveState.CREATE_UNKNOWN}:
                raise BookingError("INVALID_TRANSITION")
            return await self._write(
                tx, action.model_copy(update={"state": MoveState.NEW_CREATED, "new_visit": visit})
            )

    async def mark(self, action_id: str, state: MoveState) -> MoveAction:
        allowed = {
            MoveState.CREATE_UNKNOWN: {MoveState.CREATING, MoveState.CREATE_UNKNOWN},
            MoveState.CANCEL_UNKNOWN: {MoveState.CANCELLING, MoveState.CANCEL_UNKNOWN},
            MoveState.REJECTED: {MoveState.CREATING},
            MoveState.MANUAL: {
                MoveState.NEW_CREATED,
                MoveState.CANCELLING,
                MoveState.CANCEL_UNKNOWN,
                MoveState.MANUAL,
            },
        }
        async with self._transaction() as tx:
            action = await self._read(tx, action_id)
            if action.state == MoveState.RESCHEDULED:
                return action
            if action.state not in allowed.get(state, set()):
                raise BookingError("INVALID_TRANSITION")
            return await self._write(tx, action.model_copy(update={"state": state}))

    async def restore_unsent(self, action_id: str, turn_id: int, *, cancelling=False):
        async with self._transaction() as tx:
            action = await self._read(tx, action_id)
            if (
                action.state != (MoveState.CANCELLING if cancelling else MoveState.CREATING)
                or (action.cancel_turn if cancelling else action.create_turn) != turn_id
            ):
                raise BookingError("ACTION_NOT_UNSENT")
            return await self._write(
                tx,
                action.model_copy(
                    update={
                        "state": MoveState.NEW_CREATED if cancelling else MoveState.HELD,
                        "cancel_turn" if cancelling else "create_turn": None,
                    }
                ),
            )

    async def renew(self, action_id: str, now: datetime):
        async with self._transaction() as tx:
            action = await self._read(tx, action_id)
            if action.state != MoveState.NEW_CREATED:
                raise BookingError("NEW_VISIT_NOT_CONFIRMED")
            return await self._write(
                tx,
                action.model_copy(
                    update={
                        "revision": action.revision + 1,
                        "expires_at": now + timedelta(seconds=90),
                        "cancel_turn": None,
                    }
                ),
            )

    async def complete(self, action_id: str, old: Visit, new: Visit, now: datetime):
        async with self._transaction() as tx:
            action = await self._read(tx, action_id)
            if action.state == MoveState.RESCHEDULED:
                return action
            if (
                action.state
                not in {
                    MoveState.NEW_CREATED,
                    MoveState.CANCELLING,
                    MoveState.CANCEL_UNKNOWN,
                    MoveState.MANUAL,
                }
                or action.new_visit is None
            ):
                raise BookingError("INVALID_TRANSITION")
            if (
                not same_visit(action.before, old)
                or old.status != VisitStatus.CANCELLED
                or not matches_new(action, new)
                or not same_visit(action.new_visit, new)
            ):
                raise BookingError("MOVE_NOT_CONFIRMED")
            return await self._complete(tx, action, now)

    async def release_call(self, call_id: str):
        async with self._transaction() as tx:
            for action in await self._moves(tx, call_id):
                if action.call_id == call_id and action.state == MoveState.HELD:
                    await self._write(tx, action.model_copy(update={"state": MoveState.ABORTED}))
