"""Недоступная доставка и идемпотентный демонстрационный приём без настоящих SMS."""

from app.clock import CLINIC_TIMEZONE
from app.outbox.models import (
    CancellationSms,
    DeliveryReceipt,
    JournalEntry,
    OutboxError,
    OutboxJob,
    ReminderSms,
    RescheduleSms,
)
from app.outbox.ports import DeliveryBlocked
from app.ports import Clock
from app.storage.memory_ledger import MemoryLedger


class DisabledDelivery:
    async def deliver(self, job: OutboxJob) -> DeliveryReceipt:
        raise DeliveryBlocked()

    async def lookup(self, job: OutboxJob) -> DeliveryReceipt | None:
        return None


def journal_entry(job: OutboxJob, clock: Clock) -> JournalEntry:
    text = None
    if isinstance(job.payload, RescheduleSms):
        at = job.payload.scheduled_at.astimezone(CLINIC_TIMEZONE)
        room = f", каб. {job.payload.room}" if job.payload.room else ""
        text = (
            f"Поликлиника № 7: ваша запись перенесена на {at:%d.%m} в {at:%H:%M}{room}, "
            f"врач {job.payload.doctor_name}. Прежняя запись отменена."
        )
    if isinstance(job.payload, CancellationSms):
        at = job.payload.scheduled_at.astimezone(CLINIC_TIMEZONE)
        # Шаблон отмены из приложения Б: без специальности и сведений о пациенте.
        text = (
            f"Поликлиника № 7: запись на {at:%d.%m} в {at:%H:%M} отменена. "
            "Записаться снова: 700-07-07 или Госуслуги."
        )
    if isinstance(job.payload, ReminderSms):
        text = "Поликлиника № 7: пожалуйста, перезвоните в регистратуру."
    return JournalEntry(
        event_id=job.event_id, payload=job.payload, recorded_at=clock.now(), text=text
    )


def journal_receipt(event_id: str) -> DeliveryReceipt:
    return DeliveryReceipt(event_id=event_id, mode="demo_journal", reference=f"journal:{event_id}")


class MemoryJournalDelivery:
    def __init__(self, ledger: MemoryLedger, clock: Clock):
        self.ledger, self.clock = ledger, clock

    async def deliver(self, job: OutboxJob) -> DeliveryReceipt:
        async with self.ledger.lock:
            entry = self.ledger.deliveries.setdefault(job.event_id, journal_entry(job, self.clock))
            if entry.payload != job.payload:
                raise OutboxError("JOURNAL_CONFLICT")
        return journal_receipt(job.event_id)

    async def lookup(self, job: OutboxJob) -> DeliveryReceipt | None:
        async with self.ledger.lock:
            entry = self.ledger.deliveries.get(job.event_id)
            if entry is not None and entry.payload != job.payload:
                raise OutboxError("JOURNAL_CONFLICT")
            return journal_receipt(job.event_id) if entry is not None else None
