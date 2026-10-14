"""Отмена и освобождение местной записи завершаются в одной транзакции PostgreSQL."""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.booking.models import Approval, BookingError
from app.cancellation.guard import check_cancel_guard
from app.cancellation.models import CancelAction, CancelState, Reason, check_approval
from app.mis.models import Visit
from app.storage.journal import journal_transaction
from app.storage.outbox import enqueue_cancellation
from app.storage.schema import actions, active_moves, cancellations, dialogue_sessions, moves


def decode(row) -> CancelAction:
    before = Visit.model_validate(row.snapshot)
    if before.id != row.visit_id or before.client_id != row.client_id:
        raise BookingError("JOURNAL_CORRUPT")
    return CancelAction(
        action_id=row.action_id,
        call_id=row.call_id,
        revision=row.revision,
        before=before,
        expires_at=row.expires_at,
        state=row.state,
        confirmed_turn=row.confirmed_turn,
        reason=row.reason,
        late=bool(row.late),
        completed_at=row.completed_at,
    )


class PostgresCancellationStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        # Тот же порядок замков, что при создании: общий журнал → строка разговора.

    def _transaction(self):
        return journal_transaction(self.engine)

    async def prepare(
        self, call_id: str, before: Visit, now: datetime, reason: Reason | None = None
    ) -> CancelAction:
        async with self._transaction() as connection:
            moving = (
                await connection.execute(
                    select(moves.c.action_id).where(
                        active_moves,
                        or_(moves.c.old_visit_id == before.id, moves.c.new_visit_id == before.id),
                        or_(moves.c.state != "held", moves.c.expires_at > now),
                    )
                )
            ).first()
            if moving:
                raise BookingError("ACTION_PENDING")
            await connection.execute(
                update(cancellations)
                .where(cancellations.c.state == "prepared", cancellations.c.expires_at <= now)
                .values(state="expired")
            )
            previous = list(
                (
                    await connection.execute(
                        select(cancellations).where(cancellations.c.call_id == call_id)
                    )
                ).all()
            )
            if any(a.state in {"committing", "reconciling"} for a in previous):
                raise BookingError("ACTION_PENDING")
            pending_booking = (
                await connection.execute(
                    select(actions.c.action_id).where(
                        actions.c.state.in_(["committing", "reconciling"]),
                        actions.c.client_id == before.client_id,
                        actions.c.doctor_id == before.doctor.id,
                        actions.c.scheduled_at == before.scheduled_at,
                    )
                )
            ).first()
            if pending_booking:
                raise BookingError("ACTION_PENDING")
            other = (
                await connection.execute(
                    select(cancellations.c.action_id).where(
                        cancellations.c.visit_id == before.id,
                        cancellations.c.call_id != call_id,
                        cancellations.c.state.in_(["prepared", "committing", "reconciling"]),
                    )
                )
            ).first()
            if other:
                raise BookingError("CANCELLATION_BUSY")
            await connection.execute(
                update(cancellations)
                .where(cancellations.c.call_id == call_id, cancellations.c.state == "prepared")
                .values(state="aborted")
            )
            row = (
                await connection.execute(
                    insert(cancellations)
                    .values(
                        action_id=uuid4().hex,
                        call_id=call_id,
                        revision=max((a.revision for a in previous), default=0) + 1,
                        visit_id=before.id,
                        client_id=before.client_id,
                        snapshot=before.model_dump(mode="json"),
                        expires_at=now + timedelta(seconds=90),
                        state="prepared",
                        reason=reason,
                    )
                    .returning(cancellations)
                )
            ).one()
            return decode(row)

    async def get(self, action_id: str) -> CancelAction:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(cancellations).where(cancellations.c.action_id == action_id)
                )
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_FOUND")
            return decode(row)

    async def claim(self, approval: Approval, now: datetime) -> tuple[CancelAction, bool]:
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    select(cancellations).where(cancellations.c.action_id == approval.action_id)
                )
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_FOUND")
            action = decode(row)
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
                state = (
                    await connection.execute(
                        select(dialogue_sessions.c.state)
                        .where(dialogue_sessions.c.call_id == approval.call_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                check_cancel_guard(state, approval, action)
            row = (
                await connection.execute(
                    update(cancellations)
                    .where(cancellations.c.action_id == action.action_id)
                    .values(
                        state="committing",
                        confirmed_turn=approval.turn_id,
                        late=int(action.before.scheduled_at < now + timedelta(hours=2)),
                    )
                    .returning(cancellations)
                )
            ).one()
            return decode(row), True

    async def finish(self, action_id: str, state: CancelState, now: datetime) -> CancelAction:
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    select(cancellations).where(cancellations.c.action_id == action_id)
                )
            ).one()
            action = decode(row)
            if action.state not in {CancelState.COMMITTING, CancelState.RECONCILING}:
                if action.state == state:
                    return action
                raise BookingError("INVALID_TRANSITION")
            if state not in {CancelState.CANCELLED, CancelState.RECONCILING, CancelState.REJECTED}:
                raise BookingError("INVALID_TRANSITION")
            if state == CancelState.CANCELLED:
                # Те же commit/rollback, что у отмены и освобождения местной записи.
                await enqueue_cancellation(connection, action, now)
                await connection.execute(
                    update(actions)
                    .where(
                        actions.c.state == "booked",
                        actions.c.visit_id == action.before.id,
                        actions.c.client_id == action.before.client_id,
                        actions.c.doctor_id == action.before.doctor.id,
                        actions.c.specialty == action.before.doctor.specialty,
                        actions.c.scheduled_at == action.before.scheduled_at,
                    )
                    .values(state="archived")
                )
            row = (
                await connection.execute(
                    update(cancellations)
                    .where(cancellations.c.action_id == action_id)
                    .values(
                        state=state.value,
                        completed_at=now if state == CancelState.CANCELLED else None,
                    )
                    .returning(cancellations)
                )
            ).one()
            return decode(row)

    async def restore_unsent(self, action_id: str, turn_id: int) -> CancelAction:
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    update(cancellations)
                    .where(
                        cancellations.c.action_id == action_id,
                        cancellations.c.state == "committing",
                        cancellations.c.confirmed_turn == turn_id,
                    )
                    .values(state="prepared", confirmed_turn=None)
                    .returning(cancellations)
                )
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_UNSENT")
            return decode(row)

    async def release_call(self, call_id: str) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                update(cancellations)
                .where(cancellations.c.call_id == call_id, cancellations.c.state == "prepared")
                .values(state="aborted")
            )
