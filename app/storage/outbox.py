"""Постоянная очередь: постановка вместе с отменой, короткие аренды и квитанции."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cancellation.models import CancelAction
from app.outbox import transitions
from app.outbox.models import DeliveryReceipt, JobState, OutboxError, OutboxJob, cancellation_events
from app.storage.journal import journal_transaction
from app.storage.schema import outbox_jobs


def values(job: OutboxJob) -> dict:
    return {
        "event_id": job.event_id,
        "source_id": job.source_id,
        "kind": job.payload.kind,
        "payload": job.payload.model_dump(mode="json"),
        "created_at": job.created_at,
        "available_at": job.available_at,
        "state": job.state,
        "attempts": job.attempts,
        "lease_token": job.lease_token,
        "lease_until": job.lease_until,
        "last_error": job.last_error,
        "receipt": None if job.receipt is None else job.receipt.model_dump(mode="json"),
    }


def decode(row) -> OutboxJob:
    job = OutboxJob.model_validate({name: getattr(row, name) for name in OutboxJob.model_fields})
    if row.kind != job.payload.kind:
        raise OutboxError("JOURNAL_CORRUPT")
    return job


async def enqueue_cancellation(connection, action: CancelAction, now: datetime) -> None:
    for event in cancellation_events(action, now):
        await connection.execute(
            insert(outbox_jobs)
            .values(**values(event))
            .on_conflict_do_nothing(index_elements=[outbox_jobs.c.source_id, outbox_jobs.c.kind])
        )


class PostgresOutboxStore:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def _recover_expired(self, connection, now: datetime) -> int:
        result = await connection.execute(
            update(outbox_jobs)
            .where(outbox_jobs.c.state == "processing", outbox_jobs.c.lease_until <= now)
            .values(state="unknown", lease_token=None, lease_until=None, last_error="WORKER_LOST")
            .returning(outbox_jobs.c.event_id)
        )
        return len(result.all())

    async def recover_expired(self, now: datetime) -> int:
        async with journal_transaction(self.engine) as connection:
            return await self._recover_expired(connection, now)

    async def _get(self, connection, event_id: str) -> OutboxJob:
        row = (
            await connection.execute(select(outbox_jobs).where(outbox_jobs.c.event_id == event_id))
        ).first()
        if row is None:
            raise OutboxError("JOB_NOT_FOUND")
        return decode(row)

    async def _save(self, connection, job: OutboxJob) -> OutboxJob:
        await connection.execute(
            update(outbox_jobs).where(outbox_jobs.c.event_id == job.event_id).values(**values(job))
        )
        return job

    async def get(self, event_id: str) -> OutboxJob:
        async with self.engine.connect() as connection:
            return await self._get(connection, event_id)

    async def list(
        self, limit: int = 50, *, state: JobState | None = None
    ) -> tuple[OutboxJob, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("лимит должен быть от 1 до 100")
        async with self.engine.connect() as connection:
            query = select(outbox_jobs)
            if state is not None:
                query = query.where(outbox_jobs.c.state == state)
            rows = (
                await connection.execute(
                    query.order_by(outbox_jobs.c.created_at, outbox_jobs.c.event_id).limit(limit)
                )
            ).all()
            return tuple(decode(row) for row in rows)

    async def claim(self, now: datetime, lease_seconds: int = 30) -> OutboxJob | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("аренда должна быть от 1 до 300 секунд")
        async with journal_transaction(self.engine) as connection:
            # Просроченный обработчик мог отправить действие. Не возвращаем его в ready.
            await self._recover_expired(connection, now)
            row = (
                await connection.execute(
                    select(outbox_jobs)
                    .where(
                        outbox_jobs.c.state.in_(["pending", "retry"]),
                        outbox_jobs.c.available_at <= now,
                    )
                    .order_by(
                        outbox_jobs.c.available_at, outbox_jobs.c.created_at, outbox_jobs.c.event_id
                    )
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            return await self._save(connection, transitions.claim(decode(row), now, lease_seconds))

    async def finish(self, event_id, token, state, now, **kwargs) -> OutboxJob:
        async with journal_transaction(self.engine) as connection:
            job = await self._get(connection, event_id)
            return await self._save(
                connection, transitions.finish(job, token, state, now, **kwargs)
            )

    async def acknowledge_unknown(self, event_id: str, receipt: DeliveryReceipt) -> OutboxJob:
        async with journal_transaction(self.engine) as connection:
            job = await self._get(connection, event_id)
            return await self._save(connection, transitions.acknowledge_unknown(job, receipt))

    async def resume_blocked(self, event_id: str, now: datetime) -> OutboxJob:
        async with journal_transaction(self.engine) as connection:
            job = await self._get(connection, event_id)
            return await self._save(connection, transitions.resume_blocked(job, now))
