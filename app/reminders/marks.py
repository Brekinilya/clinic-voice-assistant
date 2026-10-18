"""Отметки С-10 в комментарии клиента: статуса «Подтверждена» у визита в API стенда нет.

PATCH заменяет комментарий целиком, поэтому строку дописываем к свежему тексту и чужое не
трогаем. Неизвестный исход PATCH проверяем чтением, повторную запись вслепую не шлём.
"""

import re
from datetime import datetime
from enum import StrEnum

from app.mis.budget import Priority, demand
from app.mis.models import COMMENT_LIMIT, ClientComment, format_date, format_datetime
from app.mis.ports import MedicalSystem, MisError, MisOutcomeUnknown
from app.ports import Clock
from app.reminders.ports import ReminderStore

OPT_OUT = "Отказ от обзвона"


class MarkResult(StrEnum):
    WRITTEN = "written"
    # Строка уже была: повтор ничего не меняет
    ALREADY = "already"
    # Без обрезки чужого текста строка не помещается: отметка только локальная
    TOO_LONG = "too_long"
    # PATCH мог пройти, проверка не подтвердила: решает человек
    UNKNOWN = "unknown"


def confirmation_line(visit_id: int, scheduled_at: datetime) -> str:
    return f"Подтверждена запись #{visit_id} {format_datetime(scheduled_at)}"


def confirmed_in(comment: str | None, visit_id: int) -> bool:
    return re.search(rf"Подтверждена запись #{visit_id}(?!\d)", comment or "") is not None


def opted_out(comment: str | None) -> bool:
    return OPT_OUT in (comment or "")


def appended(comment: str | None, line: str) -> str | None:
    text = f"{comment.rstrip()}\n{line}" if comment and comment.strip() else line
    return text if len(text) <= COMMENT_LIMIT else None


class ReminderMarks:
    """Действия, которые диалог напоминания вызывает на «да, приду» и «не звоните»."""

    def __init__(self, mis: MedicalSystem, store: ReminderStore, clock: Clock):
        self.mis = mis
        self.store = store
        self.clock = clock

    async def confirm(self, client_id: int, visit_id: int, scheduled_at: datetime) -> MarkResult:
        # Локальная отметка первой: даже без МИС второй звонок по этой записи не состоится
        await self.store.mark_confirmed(visit_id, self.clock.now())
        line = confirmation_line(visit_id, scheduled_at)
        return await self._append(client_id, line, lambda c: confirmed_in(c, visit_id))

    async def opt_out(self, client_id: int) -> MarkResult:
        now = self.clock.now()
        await self.store.opt_out(client_id, now)
        return await self._append(client_id, f"{OPT_OUT} {format_date(now.date())}", opted_out)

    async def _append(self, client_id: int, line: str, present) -> MarkResult:
        with demand(Priority.COMMIT):
            client = await self.mis.client(client_id, fresh=True)
            if present(client.comment):
                return MarkResult.ALREADY
            text = appended(client.comment, line)
            if text is None:
                return MarkResult.TOO_LONG
            try:
                await self.mis.update_client_comment(client_id, ClientComment(comment=text))
                return MarkResult.WRITTEN
            except MisOutcomeUnknown:
                pass
            try:
                check = await self.mis.client(client_id, fresh=True)
            except MisError:
                return MarkResult.UNKNOWN
            return MarkResult.WRITTEN if present(check.comment) else MarkResult.UNKNOWN
