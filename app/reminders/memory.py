"""Хранилище напоминаний в памяти: те же правила, что в PostgreSQL (app/storage/reminders.py)."""

import asyncio
from datetime import date, datetime, timedelta

from app.dialogue.reminder_guard import check_reminder_guard
from app.outbox.models import OutboxJob, reminder_sms_event
from app.recovery.models import OwnershipLost
from app.reminders.models import (
    AttemptResult,
    Contact,
    ReminderAttempt,
    ReminderJob,
    ReminderMark,
)

ACTIVE = {"claimed", "dialing", "answered"}


def channel_for(attempt_id: str) -> str:
    return f"reminder-{attempt_id}"


class MemoryReminderStore:
    def __init__(self, guard_reader=None) -> None:
        self.guard_reader = guard_reader
        self.jobs: dict[str, ReminderJob] = {}
        self.attempts: dict[str, ReminderAttempt] = {}
        self.contacts: dict[int, Contact] = {}
        self.confirmations: dict[tuple, datetime] = {}
        self.marks: dict[str, ReminderMark] = {}
        self.locks: dict[int, tuple[str, datetime]] = {}
        self.days: set[date] = set()
        self.outbox: dict[str, OutboxJob] = {}
        self._lock = asyncio.Lock()

    async def planned(self, call_day: date) -> bool:
        return call_day in self.days

    async def mark_planned(self, call_day: date) -> None:
        self.days.add(call_day)

    async def add_job(self, job: ReminderJob) -> bool:
        async with self._lock:
            if any(
                (j.visit_id, j.scheduled_at, j.doctor_id)
                == (job.visit_id, job.scheduled_at, job.doctor_id)
                for j in self.jobs.values()
            ):
                return False
            self.jobs[job.id] = job
            return True

    async def job(self, job_id: str) -> ReminderJob:
        return self.jobs[job_id]

    async def attempt(self, attempt_id: str) -> ReminderAttempt:
        return self.attempts[attempt_id]

    async def due(self, now: datetime, limit: int) -> list[ReminderJob]:
        ready = [
            j
            for j in self.jobs.values()
            if j.state in {"planned", "waiting_retry"} and j.next_at <= now
        ]
        return sorted(ready, key=lambda j: (j.next_at, j.scheduled_at))[:limit]

    async def list_jobs(self, call_day=None, state=None, limit=50) -> list[ReminderJob]:
        items = [
            j
            for j in self.jobs.values()
            if (call_day is None or j.call_day == call_day) and (state is None or j.state == state)
        ]
        return sorted(items, key=lambda j: (j.scheduled_at, j.visit_id))[:limit]

    async def claim(
        self, job_id: str, owner: str, now: datetime, lease: timedelta
    ) -> ReminderAttempt | None:
        async with self._lock:
            job = self.jobs[job_id]
            if job.state not in {"planned", "waiting_retry"} or job.next_at > now:
                return None
            mine = [a for a in self.attempts.values() if a.job_id == job_id]
            if any(a.state in ACTIVE for a in mine):
                return None
            number = job.attempts + 1
            # Номер уникален среди засчитанных попыток: несостоявшаяся номер не занимает
            if any(a.number == number and a.state != "abandoned" for a in mine):
                return None
            attempt = ReminderAttempt(
                job_id=job_id,
                number=number,
                fence=max((a.fence for a in mine), default=0) + 1,
                owner=owner,
                lease_until=now + lease,
                channel_id="",
                started_at=now,
            )
            attempt = attempt.model_copy(update={"channel_id": channel_for(attempt.id)})
            self.attempts[attempt.id] = attempt
            self.jobs[job_id] = job.model_copy(
                update={"state": "calling", "attempts": number, "version": job.version + 1}
            )
            return attempt

    async def advance(self, attempt_id, owner, state, now, lease) -> ReminderAttempt:
        async with self._lock:
            attempt = self.attempts[attempt_id]
            if attempt.owner != owner or attempt.state not in ACTIVE or attempt.lease_until <= now:
                raise OwnershipLost()
            attempt = attempt.model_copy(
                update={"lease_until": now + lease, **({"state": state} if state else {})}
            )
            self.attempts[attempt_id] = attempt
            return attempt

    async def guard(self, attempt_id: str, fence: int, now: datetime) -> bool:
        attempt = self.attempts.get(attempt_id)
        return (
            attempt is not None
            and attempt.fence == fence
            and attempt.state in ACTIVE
            and attempt.lease_until > now
        )

    async def finish(self, attempt_id, result, reason, now, job, mis_status=None) -> bool:
        async with self._lock:
            attempt = self.attempts[attempt_id]
            # Исход попытки записывается один раз: повтор события ничего не меняет
            if attempt.state not in ACTIVE:
                return False
            state = "abandoned" if result == AttemptResult.ABANDONED else "finished"
            self.attempts[attempt_id] = attempt.model_copy(
                update={
                    "state": state,
                    "result": result,
                    "mis_status": mis_status,
                    "reason": reason,
                    "finished_at": now,
                }
            )
            current = self.jobs[attempt.job_id]
            self.jobs[attempt.job_id] = current.model_copy(
                update={**job, "version": current.version + 1}
            )
            saved = self.jobs[attempt.job_id]
            if saved.sms_pending:
                event = reminder_sms_event(saved.id, saved.client_id, now)
                self.outbox.setdefault(event.event_id, event)
            return True

    async def outbox_events(self, source_id: str) -> tuple[OutboxJob, ...]:
        return tuple(event for event in self.outbox.values() if event.source_id == source_id)

    async def stale(self, now: datetime) -> list[ReminderAttempt]:
        return [a for a in self.attempts.values() if a.state in ACTIVE and a.lease_until <= now]

    async def recover(self, attempt_id: str, now: datetime) -> None:
        async with self._lock:
            attempt = self.attempts[attempt_id]
            if attempt.state not in ACTIVE or attempt.lease_until > now:
                return
            job = self.jobs[attempt.job_id]
            if attempt.state == "claimed":
                # Вызов ещё не отдавали шлюзу: попытку можно не засчитывать
                self.attempts[attempt_id] = attempt.model_copy(
                    update={"state": "abandoned", "finished_at": now}
                )
                update = {
                    "state": "planned" if attempt.number == 1 else "waiting_retry",
                    "attempts": attempt.number - 1,
                }
            else:
                # Вызов мог состояться: вслепую не перезваниваем, решает человек
                self.attempts[attempt_id] = attempt.model_copy(
                    update={"state": "unknown", "finished_at": now}
                )
                update = {"state": "manual", "reason": "unknown_" + attempt.state}
            self.jobs[job.id] = job.model_copy(update={**update, "version": job.version + 1})

    async def contact(self, client_id: int) -> Contact | None:
        return self.contacts.get(client_id)

    async def save_contact(self, contact: Contact) -> None:
        # Новый номер не снимает прежний отказ от обзвона
        previous = self.contacts.get(contact.client_id)
        if previous is not None and previous.opt_out_at is not None:
            contact = contact.model_copy(update={"opt_out_at": previous.opt_out_at})
        self.contacts[contact.client_id] = contact

    async def opt_out(self, client_id: int, now: datetime) -> None:
        contact = self.contacts.get(client_id)
        if contact is not None and contact.opt_out_at is None:
            self.contacts[client_id] = contact.model_copy(update={"opt_out_at": now})

    async def confirmed(self, visit_id: int, scheduled_at=None, doctor_id=None) -> bool:
        return any(
            key[0] == visit_id and (scheduled_at is None or key[1:] == (scheduled_at, doctor_id))
            for key in self.confirmations
        )

    async def mark_confirmed(
        self, visit_id: int, now: datetime, scheduled_at=None, doctor_id=None
    ) -> None:
        self.confirmations.setdefault((visit_id, scheduled_at or now, doctor_id or 0), now)

    async def prepare_mark(self, mark: ReminderMark) -> ReminderMark:
        """Первая запись журнала; повтор с тем же id возвращает уже существующую."""
        async with self._lock:
            return self.marks.setdefault(mark.id, mark)

    async def mark(self, mark_id: str) -> ReminderMark | None:
        return self.marks.get(mark_id)

    async def start_mark(self, mark_id, context, now, owner, approval=None):
        async with self._lock:
            mark = self.marks[mark_id]
            if approval is not None:
                if self.guard_reader is None:
                    raise OwnershipLost()
                state = await self.guard_reader.inspect(approval.call_id)
                check_reminder_guard(state.model_dump(mode="json"), approval, context, mark.kind)
            attempt = self.attempts.get(context.attempt_id)
            lock = self.locks.get(context.client_id)
            if any(
                m.id != mark_id
                and m.client_id == context.client_id
                and (
                    m.state in {"sending", "unknown"}
                    or m.state == "manual"
                    and m.reason in {"not_visible", "sending_interrupted", "not_in_response"}
                )
                for m in self.marks.values()
            ):
                raise OwnershipLost()
            if (
                attempt is None
                or attempt.fence != context.fence
                or attempt.state not in ACTIVE
                or attempt.lease_until <= now
                or lock is None
                or lock[0] != owner
                or lock[1] <= now
            ):
                raise OwnershipLost()
            if mark.state != "prepared":
                return None
            mark = mark.model_copy(update={"state": "sending", "updated_at": now})
            self.marks[mark_id] = mark
            return mark

    async def move_mark(self, mark_id: str, expected: set[str], **changes) -> ReminderMark | None:
        """Сменить состояние, только если оно всё ещё одно из ожидаемых."""
        async with self._lock:
            mark = self.marks[mark_id]
            if mark.state not in expected:
                return None
            mark = mark.model_copy(update=changes)
            self.marks[mark_id] = mark
            attempt = self.attempts.get(mark.attempt_id)
            if mark.state in {"applied", "manual"} and attempt is not None:
                if attempt.mis_status == "pending":
                    self.attempts[attempt.id] = attempt.model_copy(
                        update={"mis_status": mark.state}
                    )
                job = self.jobs[attempt.job_id]
                if job.mis_status == "pending" and job.outcome in {"CONFIRMED", "OPT_OUT"}:
                    self.jobs[job.id] = job.model_copy(
                        update={
                            "mis_status": mark.state,
                            "state": "done" if mark.state == "applied" else "manual",
                        }
                    )
            return mark

    async def marks_to_check(self, now: datetime) -> list[ReminderMark]:
        return [
            m
            for m in self.marks.values()
            if (m.state == "unknown" and m.next_check_at is not None and m.next_check_at <= now)
            or (m.state == "sending" and m.updated_at + timedelta(minutes=2) <= now)
        ]

    async def lock_client(self, client_id, owner, now, ttl) -> bool:
        async with self._lock:
            if any(
                m.client_id == client_id
                and (
                    m.state in {"sending", "unknown"}
                    or m.state == "manual"
                    and m.reason in {"not_visible", "sending_interrupted", "not_in_response"}
                )
                for m in self.marks.values()
            ):
                return False
            held = self.locks.get(client_id)
            if held is not None and held[0] != owner and held[1] > now:
                return False
            self.locks[client_id] = (owner, now + ttl)
            return True

    async def unlock_client(self, client_id, owner) -> None:
        async with self._lock:
            if self.locks.get(client_id, (None,))[0] == owner:
                del self.locks[client_id]
