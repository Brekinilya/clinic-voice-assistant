"""Хранилище задач обратного звонка. Каждый метод — одна атомарная операция."""

from datetime import datetime
from typing import Protocol

from app.callbacks.models import CallbackTask


class CallbackStore(Protocol):
    async def add(self, task: CallbackTask) -> CallbackTask:
        """Новая задача; повтор с тем же id возвращает уже существующую."""
        ...

    async def get(self, task_id: str) -> CallbackTask | None: ...
    async def move(self, task_id: str, expected: set[str], version: int, **changes):
        """Сменить поля, только если состояние и версия не изменились (иначе None)."""
        ...

    async def queue(self, now: datetime, limit: int = 50) -> list[CallbackTask]:
        """Ждущие задачи по сроку; взятые сотрудником, чей срок владения истёк, — тоже."""
        ...
