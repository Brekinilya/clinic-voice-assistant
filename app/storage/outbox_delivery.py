"""Демонстрационный приём событий в PostgreSQL; настоящих сообщений не отправляет."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.outbox.delivery import journal_entry, journal_receipt
from app.outbox.models import DeliveryReceipt, JournalEntry, OutboxError, OutboxJob
from app.ports import Clock
from app.storage.schema import delivery_journal


class PostgresJournalDelivery:
    def __init__(self, engine: AsyncEngine, clock: Clock):
        self.engine, self.clock = engine, clock

    async def deliver(self, job: OutboxJob) -> DeliveryReceipt:
        entry = journal_entry(job, self.clock)
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(delivery_journal)
                .values(event_id=job.event_id, entry=entry.model_dump(mode="json"))
                .on_conflict_do_nothing(index_elements=[delivery_journal.c.event_id])
            )
        receipt = await self.lookup(job)
        if receipt is None:
            raise OutboxError("JOURNAL_NOT_CONFIRMED")
        return receipt

    async def lookup(self, job: OutboxJob) -> DeliveryReceipt | None:
        async with self.engine.connect() as connection:
            raw = (
                await connection.execute(
                    select(delivery_journal.c.entry).where(
                        delivery_journal.c.event_id == job.event_id
                    )
                )
            ).scalar_one_or_none()
        if raw is None:
            return None
        entry = JournalEntry.model_validate(raw)
        if entry.event_id != job.event_id or entry.payload != job.payload:
            raise OutboxError("JOURNAL_CONFLICT")
        return journal_receipt(job.event_id)
