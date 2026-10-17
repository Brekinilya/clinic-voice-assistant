"""Журнал переноса под общим замком; итог и оба события сохраняются атомарно."""

from datetime import timedelta

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as upsert

from app.booking.models import BookingError
from app.cancellation.models import CancelAction, CancelState
from app.rescheduling.guard import check_move_guard
from app.rescheduling.journal import MoveJournal
from app.rescheduling.models import ACTIVE_MOVES, MoveAction, MoveState
from app.storage.booking import _decode as decode_booking
from app.storage.cancellation import decode as decode_cancel
from app.storage.journal import journal_transaction
from app.storage.outbox import enqueue_cancellation
from app.storage.schema import actions, active, cancellations, dialogue_sessions, moves


def values(action):
    return {
        "action_id": action.action_id,
        "call_id": action.call_id,
        "revision": action.revision,
        "old_visit_id": action.before.id,
        "client_id": action.before.client_id,
        "doctor_id": action.slot.doctor_id,
        "scheduled_at": action.slot.scheduled_at,
        "specialty": action.slot.specialty,
        "expires_at": action.expires_at,
        "state": action.state,
        "new_visit_id": action.new_visit.id if action.new_visit else None,
        "cancellation_action_id": action.cancellation_action_id,
        "data": action.model_dump(mode="json"),
    }


def decode(row):
    action = MoveAction.model_validate(row.data)
    for name, value in values(action).items():
        if name != "data" and getattr(row, name) != value:
            raise BookingError("JOURNAL_CORRUPT")
    return action


class PostgresMoveStore(MoveJournal):
    def __init__(self, engine):
        self.engine = engine

    def _transaction(self):
        return journal_transaction(self.engine)

    async def _read(self, tx, action_id):
        row = (await tx.execute(select(moves).where(moves.c.action_id == action_id))).first()
        if row is None:
            raise BookingError("ACTION_NOT_FOUND")
        return decode(row)

    async def _write(self, tx, action):
        data = values(action)
        await tx.execute(
            upsert(moves)
            .values(**data)
            .on_conflict_do_update(index_elements=[moves.c.action_id], set_=data)
        )
        return action

    async def _moves(self, tx, call_id):
        rows = (
            await tx.execute(
                select(moves).where(
                    or_(moves.c.call_id == call_id, moves.c.state.in_(list(ACTIVE_MOVES)))
                )
            )
        ).all()
        return tuple(decode(row) for row in rows)

    async def _bookings(self, tx):
        return tuple(
            decode_booking(row) for row in (await tx.execute(select(actions).where(active))).all()
        )

    async def _cancellations(self, tx):
        return tuple(
            decode_cancel(row)
            for row in (
                await tx.execute(
                    select(cancellations).where(
                        cancellations.c.state.in_(["prepared", "committing", "reconciling"])
                    )
                )
            ).all()
        )

    async def _guard(self, tx, approval, action):
        state = (
            await tx.execute(
                select(dialogue_sessions.c.state)
                .where(dialogue_sessions.c.call_id == approval.call_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        check_move_guard(state, approval, action)

    async def _complete(self, tx, action, now):
        before, new = action.before, action.new_visit
        cancel_revision = (
            await tx.execute(
                select(func.max(cancellations.c.revision)).where(
                    cancellations.c.call_id == action.call_id
                )
            )
        ).scalar_one() or 0
        audit = CancelAction(
            action_id=action.action_id,
            call_id=action.call_id,
            revision=cancel_revision + 1,
            before=before,
            expires_at=action.expires_at,
            state=CancelState.CANCELLED,
            confirmed_turn=action.cancel_turn,
            reason="reschedule",
            late=before.scheduled_at < now + timedelta(hours=2),
            completed_at=now,
        )
        await tx.execute(
            insert(cancellations).values(
                action_id=audit.action_id,
                call_id=audit.call_id,
                revision=audit.revision,
                visit_id=before.id,
                client_id=before.client_id,
                snapshot=before.model_dump(mode="json"),
                expires_at=audit.expires_at,
                state="cancelled",
                confirmed_turn=audit.confirmed_turn,
                reason="reschedule",
                late=int(audit.late),
                completed_at=now,
            )
        )
        await tx.execute(
            update(actions)
            .where(actions.c.state == "held", actions.c.expires_at <= now)
            .values(state="expired")
        )
        await tx.execute(
            update(actions)
            .where(
                actions.c.state == "booked",
                or_(
                    actions.c.scheduled_at < now,
                    (actions.c.visit_id == before.id)
                    & (actions.c.client_id == before.client_id)
                    & (actions.c.doctor_id == before.doctor.id)
                    & (actions.c.scheduled_at == before.scheduled_at)
                    & (actions.c.specialty == before.doctor.specialty),
                ),
            )
            .values(state="archived")
        )
        book_revision = (
            await tx.execute(
                select(func.max(actions.c.revision)).where(actions.c.call_id == action.call_id)
            )
        ).scalar_one() or 0
        await tx.execute(
            insert(actions).values(
                action_id=action.action_id,
                call_id=action.call_id,
                revision=book_revision + 1,
                client_id=before.client_id,
                doctor_id=action.slot.doctor_id,
                scheduled_at=action.slot.scheduled_at,
                specialty=action.slot.specialty,
                expires_at=action.expires_at,
                state="booked",
                confirmed_turn=action.create_turn,
                visit_id=new.id,
            )
        )
        await enqueue_cancellation(tx, audit, now, new_visit=new, room=action.room)
        return await self._write(
            tx,
            action.model_copy(
                update={"state": MoveState.RESCHEDULED, "cancellation_action_id": audit.action_id}
            ),
        )
