"""Отбор С-10: запланированные визиты следующего рабочего дня → задания напоминаний."""

import logging
from datetime import date, timedelta

from app.mis.budget import Priority, demand
from app.mis.models import VisitStatus
from app.mis.ports import MedicalSystem
from app.ports import Clock
from app.reminders.calendar import ClinicCalendar
from app.reminders.models import FRESH_VISIT, ReminderJob
from app.reminders.ports import ReminderStore

log = logging.getLogger(__name__)


class ReminderPlanner:
    def __init__(
        self, mis: MedicalSystem, store: ReminderStore, calendar: ClinicCalendar, clock: Clock
    ):
        self.mis = mis
        self.store = store
        self.calendar = calendar
        self.clock = clock

    async def plan(self, call_day: date) -> int:
        """Один раз за день звонка. Возвращает число новых заданий, которые будут звонить."""
        if not self.calendar.covers(call_day):
            # Календарь не знает этот день: рабочим его молча не считаем
            log.warning(
                "календарь обзвона не покрывает %s: дополните config/calendar.yaml", call_day
            )
            return 0
        window = self.calendar.window(call_day)
        if window is None or await self.store.planned(call_day):
            return 0
        start, end = window
        now = self.clock.now()
        visits = []
        # Фоновое чтение не вытесняет живые разговоры (T05): ждёт окна не дольше 90 секунд
        with demand(Priority.BACKGROUND, until=now + timedelta(seconds=90)):
            for day in self.calendar.visit_days(call_day):
                visits += await self.mis.visits(
                    status=VisitStatus.PLANNED, date_from=day, date_to=day
                )
        created = 0
        for visit in visits:
            if visit.scheduled_at <= now:
                continue
            job = ReminderJob(
                visit_id=visit.id,
                client_id=visit.client_id,
                doctor_id=visit.doctor.id,
                scheduled_at=visit.scheduled_at,
                call_day=call_day,
                next_at=max(start, now, visit.created_at + FRESH_VISIT),
            )
            reason = await self._skip_reason(job, end)
            if reason is not None:
                job = job.model_copy(update={"state": "skipped", "reason": reason})
            if await self.store.add_job(job) and reason is None:
                created += 1
        await self.store.mark_planned(call_day)
        log.info("напоминания на %s: визитов %s, к звонку %s", call_day, len(visits), created)
        return created

    async def _skip_reason(self, job: ReminderJob, window_end) -> str | None:
        # Отметки в комментарии клиента проверяются перед самим звонком, вместе с визитом
        if job.next_at >= window_end:
            return "created_recently"
        contact = await self.store.contact(job.client_id)
        if contact is None:
            return "no_contact"
        if contact.opt_out_at is not None:
            return "opt_out"
        if await self.store.confirmed(job.visit_id):
            return "already_confirmed"
        return None
