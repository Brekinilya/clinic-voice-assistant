"""Демонстрационный журнал и отметки событий в комментарии MIS."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.mis.budget import Priority, demand
from app.mis.models import ClientComment
from app.mis.ports import BudgetExhausted, MisError, MisOutcomeUnknown
from app.outbox.delivery import journal_entry, journal_receipt
from app.outbox.models import DeliveryReceipt, JournalEntry, OutboxError, OutboxJob
from app.outbox.ports import (
    DeliveryUnknown,
    PermanentDeliveryFailure,
    TemporaryDeliveryFailure,
)
from app.ports import Clock
from app.reminders.marks import appended, has_line
from app.storage.reminders import PostgresReminderStore
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


class PostgresClientCommentDelivery:
    """Record an event mark in the client card; this adapter does not send SMS."""

    def __init__(self, mis, reminders: PostgresReminderStore, clock: Clock):
        self.mis, self.reminders, self.clock = mis, reminders, clock

    async def deliver(self, job: OutboxJob) -> DeliveryReceipt:
        payload = job.payload
        line = getattr(payload, "expected_line", None)
        client_id = getattr(payload, "client_id", None)
        if line is None or client_id is None:
            raise PermanentDeliveryFailure("COMMENT_MARK_NOT_PREPARED")
        owner = "outbox-" + uuid4().hex
        try:
            with demand(Priority.BACKGROUND):
                async with self.mis.prepaid(2, keep_for_write=1):
                    if not await self.reminders.lock_comment_client(
                        client_id,
                        owner,
                        self.clock.now(),
                        timedelta(seconds=120),
                        event_id=job.event_id,
                    ):
                        raise TemporaryDeliveryFailure(5)
                    try:
                        if await self.reminders.notification_opted_out(client_id):
                            raise PermanentDeliveryFailure("NOTIFICATION_OPTED_OUT")
                        return await self._append(job, client_id, line)
                    finally:
                        await self.reminders.unlock_client(client_id, owner)
        except BudgetExhausted as error:
            raise TemporaryDeliveryFailure(error.retry_after) from None
        except MisOutcomeUnknown:
            raise DeliveryUnknown() from None
        except MisError as error:
            if error.code == "RATE_LIMIT":
                raise TemporaryDeliveryFailure(error.retry_after) from None
            raise PermanentDeliveryFailure("MIS_REJECTED") from None

    async def _append(self, job: OutboxJob, client_id: int, line: str) -> DeliveryReceipt:
        client = await self.mis.client(client_id, fresh=True)
        if has_line(client.comment, line):
            return self._receipt(job)
        text = appended(client.comment, line)
        if text is None:
            raise PermanentDeliveryFailure("COMMENT_TOO_LONG")
        try:
            result = await self.mis.update_client_comment(client_id, ClientComment(comment=text))
        except ValueError:
            # The MIS adapter validates comment controls before issuing the PATCH.
            raise PermanentDeliveryFailure("INVALID_COMMENT") from None
        if not has_line(result.comment, line):
            raise DeliveryUnknown()
        return self._receipt(job)

    async def lookup(self, job: OutboxJob) -> DeliveryReceipt | None:
        payload = job.payload
        line = getattr(payload, "expected_line", None)
        client_id = getattr(payload, "client_id", None)
        if line is None or client_id is None:
            return None
        try:
            with demand(Priority.RECONCILE, until=self.clock.now() + timedelta(seconds=30)):
                client = await self.mis.client(client_id, fresh=True)
        except MisError:
            return None
        return self._receipt(job) if has_line(client.comment, line) else None

    @staticmethod
    def _receipt(job: OutboxJob) -> DeliveryReceipt:
        # The mark is a local MIS comment record, not proof of SMS transmission.
        return DeliveryReceipt(
            event_id=job.event_id,
            mode="client_comment",
            reference=f"comment:{job.event_id}",
        )
