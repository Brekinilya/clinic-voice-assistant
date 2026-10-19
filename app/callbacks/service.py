"""Обратный звонок по §7.6: задачу создаёт диалог, звонит сотрудник.

Ассистент не перезванивает сам: на шлюзе нет сотрудника, которому передать разговор, и
робот-звонок не заменяет регистратора. Задача хранит срок и ссылку на карточку перевода
(заявка T08-a), сотрудник берёт её (claim), звонит и отмечает исход. До трёх попыток;
после третьей без ответа нужна SMS — канала нет, поэтому ставится флаг sms_pending.
"""

from datetime import date, datetime

from app.callbacks.models import CLAIM, MAX_ATTEMPTS, CallbackTask, deadline, promise
from app.callbacks.ports import CallbackStore
from app.ports import Clock


class CallbackService:
    def __init__(self, store: CallbackStore, clock: Clock, holidays: frozenset[date] = frozenset()):
        self.store = store
        self.clock = clock
        self.holidays = holidays

    async def schedule(
        self,
        call_id: str,
        route: str,
        priority: str,
        *,
        escalation_ref: str | None = None,
        client_id: int | None = None,
        preferred: str | None = None,
    ) -> tuple[CallbackTask, str]:
        """Задача и срок для фразы «вам перезвонят …». P1 — ValueError: только перевод и 103."""
        now = self.clock.now()
        due, out_of_hours = deadline(priority, now, self.holidays)
        task = await self.store.add(
            CallbackTask(
                id=f"{call_id}:{route}",
                call_id=call_id,
                route=route,
                priority=priority,
                escalation_ref=escalation_ref,
                client_id=client_id,
                preferred=(preferred or "").strip()[:100] or None,
                created_at=now,
                due_at=due,
                out_of_hours=out_of_hours,
            )
        )
        # Повтор того же звонка и маршрута — прежняя задача и прежний срок
        return task, promise(task, task.created_at)

    async def queue(self, limit: int = 50) -> list[CallbackTask]:
        return await self.store.queue(self.clock.now(), limit)

    async def overdue(self) -> list[CallbackTask]:
        now = self.clock.now()
        return [t for t in await self.store.queue(now, 200) if t.due_at < now]

    async def claim(self, task_id: str, owner: str) -> CallbackTask | None:
        """Сотрудник берёт задачу; чужая взятая задача доступна только после срока владения."""
        now = self.clock.now()
        task = await self.store.get(task_id)
        free = task is not None and (
            task.state == "pending"
            or (
                task.state == "calling"
                and task.claimed_until is not None
                and task.claimed_until <= now
            )
        )
        if not free:
            return None
        return await self.store.move(
            task_id,
            {"pending", "calling"},
            task.version,
            state="calling",
            owner=owner,
            claimed_until=now + CLAIM,
        )

    async def result(self, task_id: str, owner: str, answered: bool) -> CallbackTask | None:
        """Исход попытки сотрудника. Повтор отметки или чужая задача — None, без изменений."""
        task = await self.store.get(task_id)
        if task is None or task.state != "calling" or task.owner != owner:
            return None
        attempts = task.attempts + 1
        if answered:
            changes = {"state": "done"}
        elif attempts >= MAX_ATTEMPTS:
            changes = {"state": "failed", "sms_pending": True}
        else:
            changes = {"state": "pending"}
        return await self.store.move(
            task_id,
            {"calling"},
            task.version,
            attempts=attempts,
            owner=None,
            claimed_until=None,
            **changes,
        )

    @staticmethod
    def due_text(task: CallbackTask, now: datetime) -> str:
        return promise(task, now)
