"""Хранилище напоминаний в памяти: те же правила, что в PostgreSQL (app/storage/reminders.py)."""

import asyncio
from datetime import date, datetime, timedelta

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
    def __init__(self) -> None:
        self.jobs: dict[str, ReminderJob] = {}
        self.attempts: dict[str, ReminderAttempt] = {}
        self.contacts: dict[int, Contact] = {}
        self.confirmations: dict[int, datetime] = {}
        self.marks: dict[str, ReminderMark] = {}
        self.locks: dict[int, tuple[str, datetime]] = {}
        self.days: set[date] = set()
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
            return True

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

    async def confirmed(self, visit_id: int) -> bool:
        return visit_id in self.confirmations

    async def mark_confirmed(self, visit_id: int, now: datetime) -> None:
        self.confirmations.setdefault(visit_id, now)

    async def prepare_mark(self, mark: ReminderMark) -> ReminderMark:
        """Первая запись журнала; повтор с тем же id возвращает уже существующую."""
        async with self._lock:
            return self.marks.setdefault(mark.id, mark)

    async def mark(self, mark_id: str) -> ReminderMark | None:
        return self.marks.get(mark_id)

    async def move_mark(self, mark_id: str, expected: set[str], **changes) -> ReminderMark | None:
        """Сменить состояние, только если оно всё ещё одно из ожидаемых."""
        async with self._lock:
            mark = self.marks[mark_id]
            if mark.state not in expected:
                return None
            mark = mark.model_copy(update=changes)
            self.marks[mark_id] = mark
            return mark

    async def marks_to_check(self, now: datetime) -> list[ReminderMark]:
        return [
            m
            for m in self.marks.values()
            if m.state == "unknown" and m.next_check_at is not None and m.next_check_at <= now
        ]

    async def lock_client(self, client_id, owner, now, ttl) -> bool:
        async with self._lock:
            held = self.locks.get(client_id)
            if held is not None and held[0] != owner and held[1] > now:
                return False
            self.locks[client_id] = (owner, now + ttl)
            return True

    async def unlock_client(self, client_id, owner) -> None:
        async with self._lock:
            if self.locks.get(client_id, (None,))[0] == owner:
                del self.locks[client_id]
