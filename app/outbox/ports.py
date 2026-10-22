"""Очередь и доставка заменяются независимо от разговора и отмены."""

from datetime import datetime
from typing import Protocol

from app.outbox.models import DeliveryReceipt, JobState, OutboxJob


class OutboxStore(Protocol):
    async def get(self, event_id: str) -> OutboxJob: ...
    async def recover_expired(self, now: datetime) -> int: ...
    async def list(
        self, limit: int = 50, *, state: JobState | None = None
    ) -> tuple[OutboxJob, ...]: ...
    async def claim(self, now: datetime, lease_seconds: int = 30) -> OutboxJob | None: ...
    async def finish(
        self,
        event_id: str,
        token: str,
        state: JobState,
        now: datetime,
        *,
        receipt: DeliveryReceipt | None = None,
        retry_after: int = 0,
        error: str | None = None,
    ) -> OutboxJob: ...
    async def acknowledge_unknown(self, event_id: str, receipt: DeliveryReceipt) -> OutboxJob: ...
    async def resume_blocked(self, event_id: str, now: datetime) -> OutboxJob: ...


class DeliveryPort(Protocol):
    """Ключ event_id неизменен; lookup читает результат и ничего не отправляет."""

    async def deliver(self, job: OutboxJob) -> DeliveryReceipt: ...
    async def lookup(self, job: OutboxJob) -> DeliveryReceipt | None: ...


class DeliveryBlocked(Exception):
    """Адаптер отсутствует; действие заведомо не отправлено."""

    def __init__(self, code: str = "DELIVERY_NOT_CONFIGURED"):
        self.code = code[:40]


class TemporaryDeliveryFailure(Exception):
    """Получатель заведомо НЕ принял задание; безопасен ограниченный повтор."""

    def __init__(self, retry_after: int = 0):
        if type(retry_after) is not int or not 0 <= retry_after <= 3600:
            raise ValueError("задержка должна быть от 0 до 3600 секунд")
        self.retry_after = retry_after


class PermanentDeliveryFailure(Exception):
    """Известный окончательный отказ до принятия задания."""

    def __init__(self, code: str = "PERMANENT_FAILURE"):
        self.code = code


class DeliveryUnknown(Exception):
    """Действие могло быть принято: требуется lookup, а не повтор отправления."""
