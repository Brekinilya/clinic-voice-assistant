"""Исполнитель напоминаний: одна попытка за раз, исход записывается один раз.

Перед вызовом попытка переходит в dialing, и только потом шлюз получает originate. Если
процесс умер после этого, исход неизвестен: попытка становится unknown, задание — manual,
и автоматически мы не перезваниваем.
"""

import asyncio
import logging
from collections.abc import Callable, Collection
from datetime import timedelta

from app.dialogue.outbound import OutboundContext
from app.mis.budget import Priority, demand
from app.mis.models import VisitStatus
from app.mis.ports import MedicalSystem, MisError
from app.ports import Clock
from app.recovery.models import OwnershipLost
from app.reminders.calendar import ClinicCalendar
from app.reminders.marks import ReminderMarks, confirmed_in, opted_out
from app.reminders.models import AttemptResult, after_attempt
from app.reminders.planner import ReminderPlanner
from app.reminders.ports import Dialer, DialRejected, ReminderStore
from app.visits.models import VisitSummary

log = logging.getLogger(__name__)

LEASE = timedelta(seconds=60)
# Без ответа шлюза о судьбе вызова lease истечёт, и восстановление переведёт попытку в manual
MIS_WAIT = timedelta(seconds=60)

Events = Callable[[str, dict], None]


class ReminderExecutor:
    def __init__(
        self,
        store: ReminderStore,
        mis: MedicalSystem,
        calendar: ClinicCalendar,
        clock: Clock,
        dialer: Dialer,
        *,
        owner: str,
        allowed_endpoints: Collection[str],
        events: Events | None = None,
        marks: ReminderMarks | None = None,
    ):
        self.store = store
        self.mis = mis
        self.calendar = calendar
        self.clock = clock
        self.dialer = dialer
        self.owner = owner
        self.allowed = frozenset(allowed_endpoints)
        self.events = events
        self.marks = marks
        self.planner = ReminderPlanner(mis, store, calendar, clock)
        # Один софтфон: следующий звонок только после исхода текущего
        self.active: str | None = None
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            now = self.clock.now()
            await self._recover(now)
            if self.active is not None:
                await self._renew(now)
                return
            if self.marks is not None:
                await self.marks.reconcile()
            if not self.calendar.is_open(now):
                return
            await self.planner.plan(now.date())
            for job in await self.store.due(now, 1):
                attempt = await self.store.claim(job.id, self.owner, now, LEASE)
                if attempt is not None:
                    await self._start(job, attempt)

    async def run(self, interval_s: float = 15) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка исполнителя напоминаний")
            await asyncio.sleep(interval_s)

    async def answered(self, attempt_id: str) -> bool:
        try:
            await self.store.advance(attempt_id, self.owner, "answered", self.clock.now(), LEASE)
            self.active = attempt_id
            return True
        except OwnershipLost:
            log.warning("попытка %s уже не наша", attempt_id)
            return False

    async def guard(self, attempt_id: str, fence: int) -> bool:
        """Для диалога: контекст звонка всё ещё относится к действующей попытке."""
        return await self.store.guard(attempt_id, fence, self.clock.now())

    async def finished(
        self,
        attempt_id: str,
        result: str,
        reason: str | None = None,
        mis_status: str | None = None,
    ) -> bool:
        """Исход попытки: от шлюза (не ответили) или от разговора (код С-10). Повтор — no-op."""
        now = self.clock.now()
        attempt = await self.store.attempt(attempt_id)
        job = await self.store.job(attempt.job_id)
        window = self.calendar.window(job.call_day)
        update = after_attempt(
            job, attempt.number, result, reason, now, window[1] if window else None, mis_status
        )
        written = await self.store.finish(attempt_id, result, reason, now, update, mis_status)
        if self.active == attempt_id:
            self.active = None
        if written:
            log.info("напоминание %s: попытка %s — %s", job.id, attempt.number, result)
            self._emit(
                "reminder.attempt",
                job_id=job.id,
                attempt_id=attempt_id,
                number=attempt.number,
                result=result,
                mis_status=mis_status,
                reason=reason,
                job_state=update["state"],
            )
        return written

    async def _start(self, job, attempt) -> None:
        now = self.clock.now()
        if job.call_day != now.date():
            await self.finished(attempt.id, AttemptResult.SKIPPED, "call_day_passed")
            return
        try:
            with demand(Priority.BACKGROUND, until=now + MIS_WAIT):
                visit = await self.mis.visit(job.visit_id)
                if (
                    visit.status != VisitStatus.PLANNED
                    or visit.scheduled_at != job.scheduled_at
                    or visit.doctor.id != job.doctor_id
                    or visit.client_id != job.client_id
                ):
                    await self.finished(attempt.id, AttemptResult.SKIPPED, "changed")
                    return
                client = await self.mis.client(job.client_id, fresh=True)
        except MisError as error:
            # Звонка не было: попытку не засчитываем, проверим визит позже
            await self.finished(attempt.id, AttemptResult.ABANDONED, error.code)
            return
        reason = await self._skip_reason(job, client.comment)
        if reason is not None:
            await self.finished(attempt.id, AttemptResult.SKIPPED, reason)
            return
        contact = await self.store.contact(job.client_id)
        context = OutboundContext(
            attempt_id=attempt.id,
            fence=attempt.fence,
            client_id=job.client_id,
            visit=VisitSummary(
                visit_id=visit.id,
                doctor_id=visit.doctor.id,
                doctor_name=visit.doctor.full_name,
                specialty=visit.doctor.specialty,
                scheduled_at=visit.scheduled_at,
            ),
            addressee=" ".join(filter(None, (client.first_name, client.middle_name))),
        )
        try:
            await self.store.advance(attempt.id, self.owner, "dialing", self.clock.now(), LEASE)
        except OwnershipLost:
            return
        self.active = attempt.id
        try:
            await self.dialer.call_out(attempt.id, contact.endpoint, context, attempt.channel_id)
        except DialRejected:
            await self.finished(attempt.id, AttemptResult.FAILED, "originate_rejected")
        except Exception:
            # Originate мог пройти. Не продлеваем неизвестную попытку бесконечно:
            # её lease истечёт, восстановление даст manual без нового звонка.
            self.active = None
            self._emit("reminder.originate_unknown", attempt_id=attempt.id)

    async def _skip_reason(self, job, comment: str | None) -> str | None:
        if confirmed_in(comment, job.visit_id, job.scheduled_at) or await self.store.confirmed(
            job.visit_id, job.scheduled_at, job.doctor_id
        ):
            return "already_confirmed"
        contact = await self.store.contact(job.client_id)
        if contact is None:
            return "no_contact"
        if contact.opt_out_at is not None or opted_out(comment):
            return "opt_out"
        # Через шлюз хакатона можно звонить только своему софтфону
        if contact.endpoint not in self.allowed:
            return "endpoint_not_allowed"
        return None

    async def _renew(self, now) -> None:
        try:
            await self.store.advance(self.active, self.owner, None, now, LEASE)
        except OwnershipLost:
            log.warning("lease попытки %s потерян", self.active)
            self.active = None

    async def _recover(self, now) -> None:
        for attempt in await self.store.stale(now):
            await self.store.recover(attempt.id, now)
            if attempt.id == self.active:
                self.active = None
            log.warning("попытка %s без исхода после %s", attempt.id, attempt.state)
            self._emit("reminder.recovered", attempt_id=attempt.id, state=attempt.state)

    def _emit(self, kind: str, **data) -> None:
        if self.events is not None:
            try:
                self.events(kind, data)
            except Exception:
                log.exception("не удалось передать событие напоминания")
