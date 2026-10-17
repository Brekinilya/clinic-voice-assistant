"""Долговечная аренда и очередь ручного разбора, общие для всех процессов."""

from contextlib import asynccontextmanager

from sqlalchemy import and_, exists, select, update
from sqlalchemy.dialects.postgresql import insert

from app.recovery.models import RecoveryRecord, claim, owned, settle
from app.storage.schema import actions, cancellations, moves
from app.storage.schema import recovery_operations as jobs


def values(record):
    return dict(
        kind=record.kind,
        action_id=record.action_id,
        state=record.state,
        due_at=record.due_at,
        data=record.model_dump(mode="json"),
    )


def decode(row):
    record = RecoveryRecord.model_validate(row.data)
    if (record.kind, record.action_id, record.state, record.due_at) != (
        row.kind,
        row.action_id,
        row.state,
        row.due_at,
    ):
        raise RuntimeError("повреждена проекция журнала восстановления")
    return record


class PostgresRecoveryStore:
    def __init__(self, engine):
        self.engine = engine

    async def _read(self, connection, kind, action_id):
        row = (
            await connection.execute(
                select(jobs)
                .where(jobs.c.kind == kind, jobs.c.action_id == action_id)
                .with_for_update()
            )
        ).one()
        return decode(row)

    async def _save(self, connection, record):
        data = values(record)
        await connection.execute(
            update(jobs)
            .where(jobs.c.kind == record.kind, jobs.c.action_id == record.action_id)
            .values(**data)
        )
        return record

    async def seed(self, now, limit=100):
        """Подхватить старые журналы после миграции; существующую ручную очередь не сбрасывать."""
        count = 0
        async with self.engine.begin() as connection:
            for kind, source, states in (
                ("booking", actions, ["committing", "reconciling", "booked"]),
                ("cancellation", cancellations, ["committing", "reconciling"]),
                (
                    "move",
                    moves,
                    [
                        "creating",
                        "create_unknown",
                        "new_created",
                        "cancelling",
                        "cancel_unknown",
                        "manual",
                    ],
                ),
            ):
                missing = ~exists(
                    select(jobs.c.action_id).where(
                        jobs.c.kind == kind, jobs.c.action_id == source.c.action_id
                    )
                )
                identifiers = (
                    await connection.execute(
                        select(source.c.action_id, source.c.state)
                        .where(source.c.state.in_(states), missing)
                        .order_by(source.c.action_id)
                        .limit(limit - count)
                    )
                ).all()
                for action_id, state in identifiers:
                    record = RecoveryRecord(
                        kind=kind,
                        action_id=action_id,
                        due_at=now,
                        state="watching" if kind == "booking" and state == "booked" else "pending",
                    )
                    result = await connection.execute(
                        insert(jobs).values(**values(record)).on_conflict_do_nothing()
                    )
                    count += result.rowcount
                if count >= limit:
                    break
        return count

    async def acquire(self, kind, action_id, now, seconds=60):
        async with self.engine.begin() as connection:
            initial = RecoveryRecord(kind=kind, action_id=action_id, due_at=now)
            await connection.execute(
                insert(jobs).values(**values(initial)).on_conflict_do_nothing()
            )
            result = claim(await self._read(connection, kind, action_id), now, seconds)
            if result is None:
                return None
            record, lease = result
            await self._save(connection, record)
            return lease

    async def claim_next(self, now, seconds=60):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(jobs)
                    .where(
                        and_(
                            jobs.c.state.in_(["pending", "watching", "running"]),
                            jobs.c.due_at <= now,
                        )
                    )
                    .order_by(jobs.c.due_at, jobs.c.kind, jobs.c.action_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            record, lease = claim(decode(row), now, seconds, read_only=True)
            await self._save(connection, record)
            return lease

    @asynccontextmanager
    async def fence(self, lease, now, connection=None):
        if connection is None:
            async with self.engine.begin() as connection:
                owned(await self._read(connection, lease.kind, lease.action_id), lease, now)
                yield
        else:
            owned(await self._read(connection, lease.kind, lease.action_id), lease, now)
            yield

    async def finish(self, lease, now, outcome):
        async with self.engine.begin() as connection:
            current = await self._read(connection, lease.kind, lease.action_id)
            return await self._save(connection, settle(current, lease, now, outcome))

    async def list(self, limit=100, state=None):
        query = select(jobs).order_by(jobs.c.due_at, jobs.c.kind, jobs.c.action_id).limit(limit)
        if state is not None:
            query = query.where(jobs.c.state == state)
        async with self.engine.connect() as connection:
            return [decode(r) for r in (await connection.execute(query)).all()]
