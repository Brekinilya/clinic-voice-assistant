"""Задачи обратного звонка в памяти: те же правила, что в PostgreSQL."""

import asyncio

from app.callbacks.models import CallbackTask


class MemoryCallbackStore:
    def __init__(self) -> None:
        self.tasks: dict[str, CallbackTask] = {}
        self._lock = asyncio.Lock()

    async def add(self, task: CallbackTask) -> CallbackTask:
        async with self._lock:
            return self.tasks.setdefault(task.id, task)

    async def get(self, task_id: str) -> CallbackTask | None:
        return self.tasks.get(task_id)

    async def move(self, task_id, expected, version, **changes):
        async with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.state not in expected or task.version != version:
                return None
            task = task.model_copy(update={**changes, "version": version + 1})
            self.tasks[task_id] = task
            return task

    async def queue(self, now, limit=50):
        ready = [
            t
            for t in self.tasks.values()
            if t.state == "pending"
            or (t.state == "calling" and t.claimed_until is not None and t.claimed_until <= now)
        ]
        return sorted(ready, key=lambda t: (t.due_at, t.created_at))[:limit]
