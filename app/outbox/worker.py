"""Ограниченный проход без ожидания, без внешнего I/O под замком очереди."""

from app.outbox.models import DeliveryReceipt, JobState, OutboxError, OutboxJob
from app.outbox.ports import (
    DeliveryBlocked,
    DeliveryPort,
    DeliveryUnknown,
    OutboxStore,
    PermanentDeliveryFailure,
    TemporaryDeliveryFailure,
)
from app.ports import Clock


class OutboxWorker:
    def __init__(self, store: OutboxStore, delivery: DeliveryPort, clock: Clock):
        self.store, self.delivery, self.clock = store, delivery, clock

    async def run_once(self) -> OutboxJob | None:
        job = await self.store.claim(self.clock.now())
        if job is None:
            return None
        receipt, delay, error = None, 0, None
        try:
            receipt = DeliveryReceipt.model_validate(await self.delivery.deliver(job))
            if receipt.event_id != job.event_id:
                raise DeliveryUnknown()
            state = JobState.ACKNOWLEDGED
        except DeliveryBlocked:
            state, error = JobState.BLOCKED, "DELIVERY_NOT_CONFIGURED"
        except TemporaryDeliveryFailure as failure:
            state, error = JobState.RETRY, "TEMPORARY_FAILURE"
            delay = max(5 * 2 ** (job.attempts - 1), failure.retry_after)
        except PermanentDeliveryFailure as failure:
            state, error = JobState.FAILED, failure.code[:40]
        except DeliveryUnknown:
            state, error = JobState.UNKNOWN, "OUTCOME_UNKNOWN"
        except Exception:
            # В ошибку журнала не попадают телефон, текст ответа провайдера или исключения.
            state, error = JobState.UNKNOWN, "DELIVERY_FAILURE"
        if state != JobState.ACKNOWLEDGED:
            receipt = None
        return await self.store.finish(
            job.event_id,
            job.lease_token,
            state,
            self.clock.now(),
            receipt=receipt,
            retry_after=delay,
            error=error,
        )

    async def reconcile(self, event_id: str) -> OutboxJob:
        await self.store.recover_expired(self.clock.now())
        job = await self.store.get(event_id)
        if job.state == JobState.ACKNOWLEDGED:
            return job
        if job.state != JobState.UNKNOWN:
            raise OutboxError("RECONCILIATION_NOT_REQUIRED")
        try:
            receipt = await self.delivery.lookup(job)
            if receipt is not None:
                receipt = DeliveryReceipt.model_validate(receipt)
        except Exception:
            return job
        if receipt is None or receipt.event_id != event_id:
            return job
        return await self.store.acknowledge_unknown(event_id, receipt)
