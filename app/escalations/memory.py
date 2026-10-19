"""Журнал заявок в памяти: те же правила, что в PostgreSQL (app/storage/escalations.py)."""

import asyncio

from app.escalations.models import EscalationRecord


class MemoryEscalationStore:
    def __init__(self) -> None:
        self.records: dict[str, EscalationRecord] = {}
        self._lock = asyncio.Lock()

    async def prepare(self, record: EscalationRecord) -> EscalationRecord:
        async with self._lock:
            return self.records.setdefault(record.id, record)

    async def get(self, record_id: str) -> EscalationRecord | None:
        return self.records.get(record_id)

    async def move(self, record_id, expected, **changes) -> EscalationRecord | None:
        async with self._lock:
            record = self.records.get(record_id)
            if record is None or record.state not in expected:
                return None
            record = record.model_copy(update=changes)
            self.records[record_id] = record
            return record

    async def due_checks(self, now) -> list[EscalationRecord]:
        return [
            r
            for r in self.records.values()
            if r.state == "unknown" and r.next_check_at is not None and r.next_check_at <= now
        ]

    async def recent(self, limit: int = 50) -> list[EscalationRecord]:
        return sorted(self.records.values(), key=lambda r: r.created_at, reverse=True)[:limit]
