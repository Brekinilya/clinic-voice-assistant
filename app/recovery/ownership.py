"""Короткая проверка владельца: транзакция БД не держится во время HTTP."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.ports import Clock
from app.recovery.models import Lease, OwnershipLost
from app.recovery.ports import RecoveryStore


@dataclass(frozen=True)
class Scope:
    store: RecoveryStore
    lease: Lease
    clock: Clock


current: ContextVar[Scope | None] = ContextVar("recovery_owner", default=None)


@asynccontextmanager
async def ownership_fence(connection=None):
    scope = current.get()
    if scope is None:
        yield
    else:
        # Часы читаются после получения блокировки, а не перед возможным ожиданием БД.
        async with scope.store.fence(scope.lease, scope.clock.now, connection):
            yield


async def before_mutation():
    scope = current.get()
    if scope is not None:
        if scope.lease.read_only:
            raise OwnershipLost()
        async with ownership_fence():
            pass
