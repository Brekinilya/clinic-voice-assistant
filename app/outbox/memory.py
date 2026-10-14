"""Автономная очередь под тем же замком, что и завершение отмены."""

from datetime import datetime

from app.outbox import transitions
from app.outbox.models import DeliveryReceipt, JobState, OutboxError, OutboxJob
from app.storage.memory_ledger import MemoryLedger


class MemoryOutboxStore:
    def __init__(self, ledger: MemoryLedger):
        self.ledger = ledger

    def _get(self, event_id: str) -> OutboxJob:
        if event_id not in self.ledger.outbox:
            raise OutboxError("JOB_NOT_FOUND")
        return self.ledger.outbox[event_id]

    def _recover_expired(self, now: datetime) -> int:
        recovered = 0
        for key, job in self.ledger.outbox.items():
            current = transitions.expire(job, now)
            recovered += current != job
            self.ledger.outbox[key] = current
        return recovered

    async def recover_expired(self, now: datetime) -> int:
        async with self.ledger.lock:
            return self._recover_expired(now)

    async def get(self, event_id: str) -> OutboxJob:
        async with self.ledger.lock:
            return self._get(event_id)

    async def list(
        self, limit: int = 50, *, state: JobState | None = None
    ) -> tuple[OutboxJob, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("лимит должен быть от 1 до 100")
        async with self.ledger.lock:
            jobs = (j for j in self.ledger.outbox.values() if state is None or j.state == state)
            return tuple(sorted(jobs, key=lambda j: (j.created_at, j.event_id))[:limit])

    async def claim(self, now: datetime, lease_seconds: int = 30) -> OutboxJob | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("аренда должна быть от 1 до 300 секунд")
        async with self.ledger.lock:
            self._recover_expired(now)
            ready = [
                j
                for j in self.ledger.outbox.values()
                if j.state in {JobState.PENDING, JobState.RETRY} and j.available_at <= now
            ]
            if not ready:
                return None
            job = min(ready, key=lambda j: (j.available_at, j.created_at, j.event_id))
            job = transitions.claim(job, now, lease_seconds)
            self.ledger.outbox[job.event_id] = job
            return job

    async def finish(self, event_id, token, state, now, **kwargs) -> OutboxJob:
        async with self.ledger.lock:
            job = transitions.finish(self._get(event_id), token, state, now, **kwargs)
            self.ledger.outbox[event_id] = job
            return job

    async def acknowledge_unknown(self, event_id: str, receipt: DeliveryReceipt) -> OutboxJob:
        async with self.ledger.lock:
            job = transitions.acknowledge_unknown(self._get(event_id), receipt)
            self.ledger.outbox[event_id] = job
            return job

    async def resume_blocked(self, event_id: str, now: datetime) -> OutboxJob:
        async with self.ledger.lock:
            job = transitions.resume_blocked(self._get(event_id), now)
            self.ledger.outbox[event_id] = job
            return job
