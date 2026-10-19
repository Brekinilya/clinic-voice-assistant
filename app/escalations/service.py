"""Заявка на соединение со специалистом — без дублей и без ложного «заявка создана».

У стенда нет ключа уникальности: повтор того же POST создаёт вторую заявку. Поэтому:
- одна запись журнала на звонок и маршрут; повтор вызова возвращает её состояние;
- перед POST запись переходит в sending; потерянный ответ — unknown;
- unknown сверяется только чтением GET /escalations по маркеру в описании, повторного POST нет;
- заявка без идентификации — на заранее заведённого технического клиента (cli technical-client),
  клиента на критическом пути не создаём.

Стенд не соединяет и не отслеживает соединение: applied значит «заявка зафиксирована»,
а не «сотрудник ответил». Исход соединения — TransferResult телефонии.
"""

import logging
from datetime import timedelta

from app.escalations.models import (
    CHECK_AFTER,
    CHECKS,
    EscalationRecord,
    EscalationStatus,
    compose,
    digest,
    new_marker,
)
from app.escalations.ports import EscalationStore
from app.mis.budget import Priority as Budget
from app.mis.budget import demand
from app.mis.models import EscalationCreate
from app.mis.ports import MedicalSystem, MisError, MisOutcomeUnknown
from app.ports import Clock

log = logging.getLogger(__name__)


class EscalationService:
    def __init__(
        self,
        mis: MedicalSystem,
        store: EscalationStore,
        clock: Clock,
        *,
        technical_client_id: int | None = None,
    ):
        self.mis = mis
        self.store = store
        self.clock = clock
        self.technical_client_id = technical_client_id

    async def request(
        self,
        call_id: str,
        route: str,
        priority: str,
        card: str,
        client_id: int | None = None,
    ) -> EscalationStatus:
        """Создать заявку по звонку и маршруту. card — описание для специалиста, без лишних ПДн."""
        now = self.clock.now()
        target = client_id if client_id is not None else self.technical_client_id
        marker = new_marker()
        description = compose(card, marker)
        record = await self.store.prepare(
            EscalationRecord(
                id=f"{call_id}:{route}",
                call_id=call_id,
                route=route,
                priority=priority,
                client_id=target,
                technical=client_id is None,
                marker=marker,
                length=len(description),
                digest=digest(description),
                created_at=now,
                updated_at=now,
            )
        )
        if record.state != "prepared":
            # Повтор того же звонка и маршрута: вторую заявку не создаём
            return EscalationStatus.of(record)
        if record.marker != marker:
            # Прежняя попытка упала до POST (prepared): отправить безопасно, со старым маркером
            description = compose(card, record.marker)
            record = await self.store.move(
                record.id,
                {"prepared"},
                length=len(description),
                digest=digest(description),
                updated_at=now,
            )
            if record is None:
                return EscalationStatus.of(await self.store.get(f"{call_id}:{route}"))
        if record.client_id is None:
            return await self._settle(record, "manual", "no_technical_client", {"prepared"})
        if await self.store.move(record.id, {"prepared"}, state="sending", updated_at=now) is None:
            return EscalationStatus.of(await self.store.get(record.id))
        try:
            # Запись без предоплаты окна не ждёт: экстренный маршрут не задерживаем
            with demand(Budget.COMMIT):
                created = await self.mis.create_escalation(
                    EscalationCreate(client_id=record.client_id, description=description)
                )
        except MisOutcomeUnknown:
            later = self.clock.now() + CHECK_AFTER
            moved = await self.store.move(
                record.id,
                {"sending"},
                state="unknown",
                next_check_at=later,
                updated_at=self.clock.now(),
            )
            return EscalationStatus.of(moved or await self.store.get(record.id))
        except MisError as error:
            # Стенд отказал определённо (400, 429): заявки нет, повторять сами не будем
            return await self._settle(record, "manual", error.code, {"sending"})
        return await self._settle(record, "applied", None, {"sending"}, escalation_id=created.id)

    async def status(self, call_id: str, route: str) -> EscalationStatus | None:
        record = await self.store.get(f"{call_id}:{route}")
        return None if record is None else EscalationStatus.of(record)

    async def reconcile(self) -> int:
        """Неизвестные заявки — только чтение списка. Возвращает число разобранных."""
        settled = 0
        for record in await self.store.due_checks(self.clock.now()):
            now = self.clock.now()
            try:
                with demand(Budget.RECONCILE, until=now + timedelta(seconds=30)):
                    items = await self.mis.escalations(client_id=record.client_id)
                found = next(
                    (
                        e
                        for e in items
                        if record.marker in e.description and digest(e.description) == record.digest
                    ),
                    None,
                )
            except MisError:
                found = None
            checks = record.checks + 1
            if found is not None:
                changes = {"state": "applied", "escalation_id": found.id, "next_check_at": None}
            elif checks >= CHECKS:
                changes = {"state": "manual", "reason": "not_visible", "next_check_at": None}
            else:
                changes = {"next_check_at": now + CHECK_AFTER}
            moved = await self.store.move(
                record.id, {"unknown"}, checks=checks, updated_at=now, **changes
            )
            if moved is not None and moved.state != "unknown":
                settled += 1
                if moved.state == "manual":
                    log.warning("заявка %s требует ручной проверки", record.route)
        return settled

    async def _settle(self, record, state, reason, expected, **extra) -> EscalationStatus:
        moved = await self.store.move(
            record.id, expected, state=state, reason=reason, updated_at=self.clock.now(), **extra
        )
        return EscalationStatus.of(moved or await self.store.get(record.id))
