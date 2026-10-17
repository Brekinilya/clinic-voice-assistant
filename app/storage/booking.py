"""Долговечные бронь, журнал и бюджет. HTTP выполняется вне транзакций БД."""

import hashlib
import math
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.booking.guard import check_dialogue_guard
from app.booking.models import (
    ActionState,
    Approval,
    BookingAction,
    BookingError,
    Slot,
    check_approval,
)
from app.mis.ports import BudgetExhausted
from app.storage.journal import journal_transaction
from app.storage.schema import (
    SCHEMA_VERSION,
    actions,
    active,
    active_moves,
    budget_blocks,
    budget_requests,
    dialogue_sessions,
    metadata,
    moves,
    schema_versions,
)


async def migrate(engine: AsyncEngine) -> None:
    """Миграции 1/2/3/4 → 5 сохраняют прежние журналы и добавляют перенос."""
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(142001)"))
        await connection.run_sync(metadata.create_all)
        versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
        if versions not in ([], [1], [2], [3], [4], [SCHEMA_VERSION]):
            raise RuntimeError("неподдерживаемая версия схемы backend")
        if not versions:
            await connection.execute(insert(schema_versions).values(version=SCHEMA_VERSION))
        elif versions in ([1], [2], [3], [4]):
            checks = await connection.run_sync(
                lambda sync: inspect(sync).get_check_constraints("backend_outbox_jobs")
            )
            kinds = [
                c
                for c in checks
                if "cancellation_sms" in c["sqltext"] and "slot_released" in c["sqltext"]
            ]
            if len(kinds) != 1:
                raise RuntimeError("неизвестное ограничение видов событий")
            if "reschedule_sms" not in kinds[0]["sqltext"]:
                name = connection.dialect.identifier_preparer.quote(kinds[0]["name"])
                await connection.execute(
                    text(f"ALTER TABLE backend_outbox_jobs DROP CONSTRAINT {name}")
                )
                await connection.execute(
                    text(
                        "ALTER TABLE backend_outbox_jobs ADD CONSTRAINT backend_outbox_kind_allowed CHECK (kind IN ('cancellation_sms','slot_released','reschedule_sms'))"
                    )
                )
            await connection.execute(update(schema_versions).values(version=SCHEMA_VERSION))


def _decode(row) -> BookingAction:
    return BookingAction(
        action_id=row.action_id,
        call_id=row.call_id,
        revision=row.revision,
        client_id=row.client_id,
        slot=Slot(doctor_id=row.doctor_id, scheduled_at=row.scheduled_at, specialty=row.specialty),
        expires_at=row.expires_at,
        state=row.state,
        confirmed_turn=row.confirmed_turn,
        visit_id=row.visit_id,
    )


class PostgresBookingStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    def _transaction(self):
        return journal_transaction(self.engine)

    async def _expire(self, connection, now: datetime) -> None:
        await connection.execute(
            text(
                "UPDATE backend_reschedule_actions SET state = 'expired', data = jsonb_set(data, '{state}', '\"expired\"'::jsonb) WHERE state = 'held' AND expires_at <= :now"
            ),
            {"now": now},
        )
        await connection.execute(
            update(actions)
            .where(actions.c.state == "held", actions.c.expires_at <= now)
            .values(state="expired")
        )
        await connection.execute(
            update(actions)
            .where(actions.c.state == "booked", actions.c.scheduled_at < now)
            .values(state="archived")
        )

    async def available(self, slot: Slot, now: datetime) -> bool:
        async with self._transaction() as connection:
            await self._expire(connection, now)
            result = await connection.execute(
                select(actions.c.action_id).where(
                    active,
                    actions.c.doctor_id == slot.doctor_id,
                    actions.c.scheduled_at == slot.scheduled_at,
                )
            )
            other = (
                await connection.execute(
                    select(moves.c.action_id).where(
                        active_moves,
                        moves.c.doctor_id == slot.doctor_id,
                        moves.c.scheduled_at == slot.scheduled_at,
                    )
                )
            ).first()
            return result.first() is None and other is None

    async def hold(self, call_id: str, client_id: int, slot: Slot, now: datetime) -> BookingAction:
        async with self._transaction() as connection:
            await self._expire(connection, now)
            previous = list(
                (
                    await connection.execute(select(actions).where(actions.c.call_id == call_id))
                ).all()
            )
            other = (
                await connection.execute(
                    select(moves.c.action_id).where(
                        active_moves,
                        or_(
                            (moves.c.doctor_id == slot.doctor_id)
                            & (moves.c.scheduled_at == slot.scheduled_at),
                            (moves.c.client_id == client_id)
                            & (moves.c.specialty == slot.specialty),
                        ),
                    )
                )
            ).first()
            if other:
                raise BookingError("ACTION_PENDING")
            if any(a.state in {"committing", "reconciling"} for a in previous):
                raise BookingError("ACTION_PENDING")
            await connection.execute(
                update(actions)
                .where(actions.c.call_id == call_id, actions.c.state == "held")
                .values(state="cancelled")
            )
            occupied = list((await connection.execute(select(actions).where(active))).all())
            if any(
                a.doctor_id == slot.doctor_id and a.scheduled_at == slot.scheduled_at
                for a in occupied
            ):
                raise BookingError("SLOT_BUSY")
            if any(a.client_id == client_id and a.specialty == slot.specialty for a in occupied):
                raise BookingError("SPECIALTY_ALREADY_BOOKED")
            row = (
                await connection.execute(
                    insert(actions)
                    .values(
                        action_id=uuid4().hex,
                        call_id=call_id,
                        revision=max((a.revision for a in previous), default=0) + 1,
                        client_id=client_id,
                        doctor_id=slot.doctor_id,
                        scheduled_at=slot.scheduled_at,
                        specialty=slot.specialty,
                        expires_at=now + timedelta(seconds=90),
                        state="held",
                    )
                    .returning(actions)
                )
            ).one()
            return _decode(row)

    async def get(self, action_id: str) -> BookingAction:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(select(actions).where(actions.c.action_id == action_id))
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_FOUND")
            return _decode(row)

    async def claim(self, approval: Approval, now: datetime) -> tuple[BookingAction, bool]:
        async with self._transaction() as connection:
            await self._expire(connection, now)
            row = (
                await connection.execute(
                    select(actions).where(actions.c.action_id == approval.action_id)
                )
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_FOUND")
            action = _decode(row)
            check_approval(action, approval)
            if approval.guard_version is not None:
                # Блокировку строки разговора держим до commit COMMITTING в той же транзакции.
                state = (
                    await connection.execute(
                        select(dialogue_sessions.c.state)
                        .where(dialogue_sessions.c.call_id == approval.call_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                check_dialogue_guard(state, approval)
            if action.state in {
                ActionState.ARCHIVED,
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }:
                return action, False
            if action.state != ActionState.HELD:
                raise BookingError("HOLD_NOT_ACTIVE")
            row = (
                await connection.execute(
                    update(actions)
                    .where(actions.c.action_id == approval.action_id)
                    .values(state="committing", confirmed_turn=approval.turn_id)
                    .returning(actions)
                )
            ).one()
            return _decode(row), True

    async def finish(
        self, action_id: str, state: ActionState, visit_id: int | None = None
    ) -> BookingAction:
        async with self._transaction() as connection:
            row = (
                await connection.execute(select(actions).where(actions.c.action_id == action_id))
            ).one()
            if row.state not in {"committing", "reconciling"}:
                if row.state == state.value and row.visit_id == visit_id:
                    return _decode(row)
                raise BookingError("INVALID_TRANSITION")
            if state not in {ActionState.BOOKED, ActionState.REJECTED, ActionState.RECONCILING}:
                raise BookingError("INVALID_TRANSITION")
            if (state == ActionState.BOOKED) != (visit_id is not None):
                raise BookingError("VISIT_ID_REQUIRED")
            row = (
                await connection.execute(
                    update(actions)
                    .where(actions.c.action_id == action_id)
                    .values(state=state.value, visit_id=visit_id)
                    .returning(actions)
                )
            ).one()
            return _decode(row)

    async def cancel(self, action_id: str, call_id: str) -> BookingAction:
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    update(actions)
                    .where(
                        actions.c.action_id == action_id,
                        actions.c.call_id == call_id,
                        actions.c.state == "held",
                    )
                    .values(state="cancelled")
                    .returning(actions)
                )
            ).first()
            if row is None:
                raise BookingError("HOLD_NOT_ACTIVE")
            return _decode(row)

    async def restore_unsent(self, action_id: str, confirmed_turn: int) -> BookingAction:
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    update(actions)
                    .where(
                        actions.c.action_id == action_id,
                        actions.c.state == "committing",
                        actions.c.confirmed_turn == confirmed_turn,
                    )
                    .values(state="held", confirmed_turn=None)
                    .returning(actions)
                )
            ).first()
            if row is None:
                raise BookingError("ACTION_NOT_UNSENT")
            return _decode(row)

    async def release_call_holds(self, call_id: str) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                update(actions)
                .where(actions.c.call_id == call_id, actions.c.state == "held")
                .values(state="cancelled")
            )


class PostgresBudget:
    def __init__(self, engine: AsyncEngine, token: str, limit: int = 8) -> None:
        if not token.strip() or not 1 <= limit <= 8:
            raise ValueError("нужны токен и бюджет от 1 до 8")
        self.engine = engine
        self.scope = hashlib.sha256(token.encode()).hexdigest()
        self.limit = limit

    async def _locked(self, connection) -> datetime:
        now = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
        await connection.execute(
            pg_insert(budget_blocks)
            .values(scope=self.scope, blocked_until=now)
            .on_conflict_do_nothing()
        )
        await connection.execute(
            select(budget_blocks).where(budget_blocks.c.scope == self.scope).with_for_update()
        )
        return (await connection.execute(select(func.clock_timestamp()))).scalar_one()

    async def reserve(self) -> None:
        async with self.engine.begin() as connection:
            now = await self._locked(connection)
            await connection.execute(
                delete(budget_requests).where(
                    budget_requests.c.scope == self.scope,
                    budget_requests.c.requested_at <= now - timedelta(seconds=60),
                )
            )
            blocked = (
                await connection.execute(
                    select(budget_blocks.c.blocked_until).where(budget_blocks.c.scope == self.scope)
                )
            ).scalar_one()
            requests = list(
                (
                    await connection.execute(
                        select(budget_requests.c.requested_at)
                        .where(budget_requests.c.scope == self.scope)
                        .order_by(budget_requests.c.requested_at)
                    )
                ).scalars()
            )
            delay = (blocked - now).total_seconds()
            if len(requests) >= self.limit:
                delay = max(delay, 60 - (now - requests[0]).total_seconds())
            if delay > 0:
                raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=max(1, math.ceil(delay)))
            await connection.execute(
                insert(budget_requests).values(scope=self.scope, requested_at=now)
            )

    async def defer(self, seconds: int) -> None:
        async with self.engine.begin() as connection:
            now = await self._locked(connection)
            await connection.execute(
                update(budget_blocks)
                .where(budget_blocks.c.scope == self.scope)
                .values(
                    blocked_until=func.greatest(
                        budget_blocks.c.blocked_until, now + timedelta(seconds=seconds)
                    )
                )
            )
