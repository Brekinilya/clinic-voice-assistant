"""Контракт очереди одинаков для автономного стенда и PostgreSQL."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from app.recovery.models import Kind, Lease, Outcome, RecoveryRecord, Status

Now = datetime | Callable[[], datetime]


class RecoveryStore(Protocol):
    async def seed(self, now: datetime, limit: int = 100) -> int: ...
    async def acquire(
        self, kind: Kind, action_id: str, now: datetime, seconds: int = 60
    ) -> Lease | None: ...
    async def claim_next(self, now: datetime, seconds: int = 60) -> Lease | None: ...
    def fence(self, lease: Lease, now: Now, connection=None) -> AbstractAsyncContextManager: ...
    async def finish(self, lease: Lease, now: Now, outcome: Outcome) -> RecoveryRecord: ...
    async def list(self, limit: int = 100, state: Status | None = None) -> list[RecoveryRecord]: ...
