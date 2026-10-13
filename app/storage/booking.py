"""Долговечные бронь, журнал и бюджет. HTTP выполняется вне транзакций БД."""

import hashlib
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.booking.models import (
    ACTIVE_STATES,
    ActionState,
    Approval,
    BookingAction,
    BookingError,
    Slot,
    check_approval,
)
from app.mis.ports import MisError

metadata = MetaData()
schema_versions = Table("backend_schema", metadata, Column("version", Integer, primary_key=True))
actions = Table(
    "backend_booking_actions",
    metadata,
    Column("action_id", String(32), primary_key=True),
    Column("call_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("doctor_id", Integer, nullable=False),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("specialty", String(100), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(20), nullable=False),
    Column("confirmed_turn", Integer),
    Column("visit_id", Integer),
    CheckConstraint("revision > 0 AND client_id > 0 AND doctor_id > 0"),
    CheckConstraint(
        "state IN ('held','committing','reconciling','booked','archived','rejected','expired','cancelled')"
    ),
    CheckConstraint("(state IN ('booked','archived')) = (visit_id IS NOT NULL)"),
)
active = actions.c.state.in_([s.value for s in ACTIVE_STATES])
Index(
    "backend_active_slot",
    actions.c.doctor_id,
    actions.c.scheduled_at,
    unique=True,
    postgresql_where=active,
)
Index(
    "backend_active_specialty",
    actions.c.client_id,
    actions.c.specialty,
    unique=True,
    postgresql_where=active,
)
Index("backend_call_revision", actions.c.call_id, actions.c.revision, unique=True)
budget_blocks = Table(
    "mis_budget_blocks",
    metadata,
    Column("scope", String(64), primary_key=True),
    Column("blocked_until", DateTime(timezone=True), nullable=False),
)
budget_requests = Table(
    "mis_budget_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("scope", String(64), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
)
Index("mis_budget_window", budget_requests.c.scope, budget_requests.c.requested_at)


async def migrate(engine: AsyncEngine) -> None:
    """Первую схему создаём явной командой; изменений существующей схемы здесь нет."""
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(142001)"))
        await connection.run_sync(metadata.create_all)
        versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
        if not versions:
            await connection.execute(insert(schema_versions).values(version=1))
        elif versions != [1]:
            raise RuntimeError("неподдерживаемая версия схемы backend")


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

    @asynccontextmanager
    async def _transaction(self):
        async with self.engine.begin() as connection:
            # При лимите МИС 8/мин одна короткая блокировка проще порядка множества замков.
            # Замок общий для всех процессов; снимается при commit/rollback/разрыве соединения.
            await connection.execute(text("SELECT pg_advisory_xact_lock(142002)"))
            yield connection

    async def _expire(self, connection, now: datetime) -> None:
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
            return result.first() is None

    async def hold(self, call_id: str, client_id: int, slot: Slot, now: datetime) -> BookingAction:
        async with self._transaction() as connection:
            await self._expire(connection, now)
            previous = list(
                (
                    await connection.execute(select(actions).where(actions.c.call_id == call_id))
                ).all()
            )
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
                raise MisError("LOCAL_RATE_LIMIT", retry_after=max(1, math.ceil(delay)))
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
