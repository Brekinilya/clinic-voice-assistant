"""Отметки С-10 в комментарии клиента: статуса «Подтверждена» у визита в API стенда нет.

PATCH заменяет комментарий целиком: строку дописываем к свежему тексту, прежний текст храним
дословно. Каждая отметка ведёт журнал prepared → sending → applied | unknown | manual.
После неизвестного исхода PATCH только читаем: отсутствие строки на одном чтении не доказывает,
что запрос не завершится позже, поэтому повторного PATCH нет никогда.

Ограничение стенда: условного PATCH (If-Match/ETag) в API нет. Блокировка по клиенту защищает
только наших писателей; правку, сделанную в тот же момент вне системы, исключить нельзя.
"""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from app.dialogue.outbound import OutboundContext
from app.mis.budget import Priority, demand
from app.mis.models import COMMENT_LIMIT, ClientComment, VisitStatus, format_date, format_datetime
from app.mis.ports import MedicalSystem, MisError, MisOutcomeUnknown
from app.ports import Clock
from app.recovery.models import OwnershipLost
from app.reminders.models import MARK_CHECK_AFTER, MARK_CHECKS, MisStatus, ReminderMark
from app.reminders.ports import ReminderStore

log = logging.getLogger(__name__)

OPT_OUT = "Отказ от обзвона"
NOTIFICATION_OPT_OUT = "Отказ от уведомлений"
LOCK_TTL = timedelta(seconds=120)
LOCK_TRIES = 10


def confirmation_line(visit_id: int, scheduled_at) -> str:
    """Дата и время самого визита по времени клиники."""
    return f"Подтверждена запись #{visit_id} {format_datetime(scheduled_at)}"


def lines(comment: str | None) -> list[str]:
    return [line.rstrip("\r") for line in (comment or "").split("\n")]


def has_line(comment: str | None, line: str) -> bool:
    # Целая строка: «#1 …» не совпадёт с «#10 …», другое время визита — тоже
    return line in lines(comment)


def confirmed_in(comment: str | None, visit_id: int, scheduled_at) -> bool:
    return has_line(comment, confirmation_line(visit_id, scheduled_at))


def opted_out(comment: str | None) -> bool:
    return any(line.startswith((OPT_OUT, NOTIFICATION_OPT_OUT)) for line in lines(comment))


def appended(comment: str | None, line: str) -> str | None:
    """Прежний текст дословно и одна строка; если не помещается — None, ничего не обрезаем."""
    if not comment:
        text = line
    elif comment.endswith("\n"):
        text = comment + line
    else:
        text = comment + "\n" + line
    return text if len(text) <= COMMENT_LIMIT else None


class ReminderMarks:
    """Действия, которые диалог напоминания вызывает на «да, приду» и «не звоните мне»."""

    def __init__(
        self,
        mis: MedicalSystem,
        store: ReminderStore,
        clock: Clock,
        *,
        owner: str,
        require_dialogue_guard: bool = False,
    ):
        self.mis = mis
        self.store = store
        self.clock = clock
        self.owner = owner
        self.require_dialogue_guard = require_dialogue_guard

    async def confirm(self, context: OutboundContext, approval=None) -> MisStatus:
        if self.require_dialogue_guard and approval is None:
            raise OwnershipLost()
        visit = context.visit
        # Локальный факт первым: второй звонок по этой записи не состоится даже без МИС
        await self.store.mark_confirmed(
            visit.visit_id, self.clock.now(), visit.scheduled_at, visit.doctor_id
        )
        mark = self._mark(context, "confirm", confirmation_line(visit.visit_id, visit.scheduled_at))
        return await self._apply(mark, context, check_visit=True, approval=approval)

    async def opt_out(self, context: OutboundContext, approval=None) -> MisStatus:
        if self.require_dialogue_guard and approval is None:
            raise OwnershipLost()
        now = self.clock.now()
        # Локальный запрет сразу: будущие задания прекращаются даже при недоступной МИС
        await self.store.opt_out(context.client_id, now)
        mark = self._mark(context, "opt_out", f"{OPT_OUT} {format_date(now.date())}")
        return await self._apply(mark, context, check_visit=False, approval=approval)

    async def opt_out_all(self, context: OutboundContext, approval=None) -> MisStatus:
        if self.require_dialogue_guard and approval is None:
            raise OwnershipLost()
        now = self.clock.now()
        # Сразу запрещаем звонки и подавляем все ещё не начатые SMS-напоминания.
        await self.store.opt_out_all(context.client_id, now)
        mark = self._mark(
            context, "opt_out_all", f"{NOTIFICATION_OPT_OUT} {format_date(now.date())}"
        )
        return await self._apply(mark, context, check_visit=False, approval=approval)

    async def reconcile(self) -> int:
        """Сверка неизвестных отметок — только чтение. Возвращает число разобранных."""
        settled = 0
        for mark in await self.store.marks_to_check(self.clock.now()):
            now = self.clock.now()
            try:
                with demand(Priority.RECONCILE, until=now + timedelta(seconds=30)):
                    client = await self.mis.client(mark.client_id, fresh=True)
                visible = has_line(client.comment, mark.line)
            except MisError:
                visible = False
            checks = mark.checks + 1
            if visible:
                changes = {"state": "applied", "checks": checks, "next_check_at": None}
            elif checks >= MARK_CHECKS:
                changes = {"state": "manual", "checks": checks, "reason": "not_visible"}
            else:
                changes = {
                    "state": "unknown",
                    "checks": checks,
                    "next_check_at": now + MARK_CHECK_AFTER,
                }
            moved = await self.store.move_mark(
                mark.id, {"unknown", "sending"}, updated_at=now, **changes
            )
            if moved is not None and moved.state != "unknown":
                settled += 1
        return settled

    def _mark(self, context: OutboundContext, kind: str, line: str) -> ReminderMark:
        return ReminderMark(
            id=f"{kind}:{context.attempt_id}",
            attempt_id=context.attempt_id,
            kind=kind,
            client_id=context.client_id,
            visit_id=context.visit.visit_id if kind == "confirm" else None,
            line=line,
            updated_at=self.clock.now(),
        )

    async def _apply(
        self, mark: ReminderMark, context, *, check_visit: bool, approval=None
    ) -> MisStatus:
        mark = await self.store.prepare_mark(mark)
        if mark.state != "prepared":
            # Повтор доставки результата: используем уже существующую запись журнала
            return mark.mis_status
        # Бюджет ждём до блокировки клиента и чтения комментария. Между GET и PATCH
        # ожидания окна уже нет, а guard атомарно сверяется с журналом sending.
        with demand(Priority.COMMIT):
            async with self.mis.prepaid(3 if check_visit else 2, keep_for_write=1):
                owner = "mark-" + uuid4().hex
                if not await self._lock(mark.client_id, owner):
                    return await self._manual(mark, "client_locked")
                try:
                    return await self._write(mark, context, check_visit, owner, approval)
                finally:
                    await self.store.unlock_client(mark.client_id, owner)

    async def _write(
        self, mark: ReminderMark, context, check_visit: bool, owner, approval
    ) -> MisStatus:
        try:
            with demand(Priority.COMMIT):
                if check_visit and not await self._visit_is_current(context):
                    # Визит отменили или перенесли после originate: старый разговор его не подтвердит
                    return await self._manual(mark, "visit_changed")
                client = await self.mis.client(mark.client_id, fresh=True)
        except MisError as error:
            return await self._manual(mark, error.code)
        if has_line(client.comment, mark.line):
            return await self._settle(mark, "applied", None)
        text = appended(client.comment, mark.line)
        if text is None:
            return await self._manual(mark, "comment_too_long")
        if not await self.store.guard(context.attempt_id, context.fence, self.clock.now()):
            return await self._manual(mark, "attempt_not_current")
        if await self.store.start_mark(mark.id, context, self.clock.now(), owner, approval) is None:
            return (await self.store.mark(mark.id)).mis_status
        try:
            with demand(Priority.COMMIT):
                result = await self.mis.update_client_comment(
                    mark.client_id, ClientComment(comment=text)
                )
        except MisOutcomeUnknown:
            now = self.clock.now()
            await self.store.move_mark(
                mark.id,
                {"sending"},
                state="unknown",
                next_check_at=now + MARK_CHECK_AFTER,
                updated_at=now,
            )
            return "pending"
        except MisError as error:
            # Стенд отказал определённо: запрос не применён, но повторять его сами не будем
            return await self._settle(mark, "manual", error.code, expected={"sending"})
        if not has_line(result.comment, mark.line):
            now = self.clock.now()
            await self.store.move_mark(
                mark.id,
                {"sending"},
                state="unknown",
                reason="not_in_response",
                next_check_at=now + MARK_CHECK_AFTER,
                updated_at=now,
            )
            return "pending"
        return await self._settle(mark, "applied", None, expected={"sending"})

    async def _visit_is_current(self, context: OutboundContext) -> bool:
        expected = context.visit
        visit = await self.mis.visit(expected.visit_id)
        return (
            visit.client_id == context.client_id
            and visit.status == VisitStatus.PLANNED
            and visit.scheduled_at == expected.scheduled_at
            and visit.doctor.id == expected.doctor_id
        )

    async def _lock(self, client_id: int, owner) -> bool:
        for _ in range(LOCK_TRIES):
            if await self.store.lock_client(client_id, owner, self.clock.now(), LOCK_TTL):
                return True
            await asyncio.sleep(0.2)
        return False

    async def _manual(self, mark: ReminderMark, reason: str) -> MisStatus:
        return await self._settle(mark, "manual", reason)

    async def _settle(self, mark, state, reason, expected=frozenset({"prepared"})) -> MisStatus:
        moved = await self.store.move_mark(
            mark.id, set(expected), state=state, reason=reason, updated_at=self.clock.now()
        )
        if moved is None:
            moved = await self.store.mark(mark.id)
        if moved.state == "manual":
            log.warning("отметка %s требует ручной проверки: %s", mark.kind, moved.reason)
        return moved.mis_status
