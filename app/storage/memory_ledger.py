"""Общий замок журналов создания и отмены для автономного процесса."""

import asyncio
from typing import TYPE_CHECKING

from app.recovery.ownership import ownership_fence

if TYPE_CHECKING:
    from app.booking.models import BookingAction
    from app.cancellation.models import CancelAction
    from app.outbox.models import JournalEntry, OutboxJob
    from app.rescheduling.models import MoveAction


class JournalLock:
    """Тестовый аналог транзакции: сменить владельца посреди записи нельзя."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._fence = None

    async def __aenter__(self):
        await self._lock.acquire()
        self._fence = ownership_fence()
        try:
            await self._fence.__aenter__()
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *args):
        try:
            await self._fence.__aexit__(*args)
        finally:
            self._fence = None
            self._lock.release()


class MemoryLedger:
    def __init__(self) -> None:
        self.lock = JournalLock()
        self.bookings: dict[str, BookingAction] = {}
        self.cancellations: dict[str, CancelAction] = {}
        self.outbox: dict[str, OutboxJob] = {}
        self.deliveries: dict[str, JournalEntry] = {}
        self.moves: dict[str, MoveAction] = {}
