"""Хранилище журнала заявок. Каждый метод — одна атомарная операция."""

from datetime import datetime
from typing import Protocol

from app.escalations.models import EscalationRecord


class EscalationStore(Protocol):
    async def prepare(self, record: EscalationRecord) -> EscalationRecord:
        """Первая запись по ключу; повтор с тем же id возвращает уже существующую."""
        ...

    async def get(self, record_id: str) -> EscalationRecord | None: ...
    async def move(self, record_id: str, expected: set[str], **changes) -> EscalationRecord | None:
        """Сменить поля, только если состояние всё ещё одно из ожидаемых."""
        ...

    async def due_checks(self, now: datetime) -> list[EscalationRecord]: ...
    async def recent(self, limit: int = 50) -> list[EscalationRecord]: ...
