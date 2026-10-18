"""Задания и попытки напоминаний в PostgreSQL: общие для процессов, переживают перезапуск.

Каждый метод — одна транзакция. Гонку двух исполнителей за одну попытку решают уникальные
индексы: проигравший получает None, а не второй звонок.
"""

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.recovery.models import OwnershipLost
from app.reminders.models import AttemptResult, Contact, ReminderAttempt, ReminderJob
from app.storage.schema import (
    reminder_attempts as attempts,
)
from app.storage.schema import (
    reminder_confirmations as confirmations,
)
from app.storage.schema import (
    reminder_contacts as contacts,
)
from app.storage.schema import (
    reminder_days as days,
)
from app.storage.schema import (
    reminder_jobs as jobs,
)

ACTIVE = ("claimed", "dialing", "answered")


def _job(row) -> ReminderJob:
    return ReminderJob.model_validate(row._asdict())


def _attempt(row) -> ReminderAttempt:
    return ReminderAttempt.model_validate(row._asdict())


class PostgresReminderStore:
    def __init__(self, engine):
        self.engine = engine

    async def planned(self, call_day):
        async with self.engine.connect() as connection:
            row = await connection.execute(select(days).where(days.c.call_day == call_day))
            return row.first() is not None

    async def mark_planned(self, call_day):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(days).values(call_day=call_day).on_conflict_do_nothing()
            )

    async def add_job(self, job):
        async with self.engine.begin() as connection:
            result = await connection.execute(
                insert(jobs).values(**job.model_dump()).on_conflict_do_nothing()
            )
            return result.rowcount == 1

    async def job(self, job_id):
        async with self.engine.connect() as connection:
            return _job((await connection.execute(select(jobs).where(jobs.c.id == job_id))).one())

    async def attempt(self, attempt_id):
        async with self.engine.connect() as connection:
            row = await connection.execute(select(attempts).where(attempts.c.id == attempt_id))
            return _attempt(row.one())

    async def due(self, now, limit):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(jobs)
                .where(jobs.c.state.in_(["planned", "waiting_retry"]), jobs.c.next_at <= now)
                .order_by(jobs.c.next_at, jobs.c.scheduled_at)
                .limit(limit)
            )
            return [_job(row) for row in rows]

    async def list_jobs(self, call_day=None, state=None, limit=50):
        query = select(jobs).order_by(jobs.c.scheduled_at, jobs.c.visit_id).limit(limit)
        if call_day is not None:
            query = query.where(jobs.c.call_day == call_day)
        if state is not None:
            query = query.where(jobs.c.state == state)
        async with self.engine.connect() as connection:
            return [_job(row) for row in await connection.execute(query)]

    async def claim(self, job_id, owner, now, lease):
        try:
            async with self.engine.begin() as connection:
                row = (
                    await connection.execute(
                        select(jobs).where(jobs.c.id == job_id).with_for_update()
                    )
                ).one()
                job = _job(row)
                if job.state not in {"planned", "waiting_retry"} or job.next_at > now:
                    return None
                number = job.attempts + 1
                attempt = ReminderAttempt(
                    job_id=job_id,
                    number=number,
                    owner=owner,
                    lease_until=now + lease,
                    started_at=now,
                )
                await connection.execute(insert(attempts).values(**attempt.model_dump()))
                await connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(state="calling", attempts=number, version=jobs.c.version + 1)
                )
                return attempt
        except IntegrityError:
            # Уникальные индексы: у задания уже есть активная попытка или этот номер занят
            return None

    async def advance(self, attempt_id, owner, state, now, lease):
        values = {"lease_until": now + lease, **({"state": state} if state else {})}
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(attempts)
                    .where(
                        attempts.c.id == attempt_id,
                        attempts.c.owner == owner,
                        attempts.c.state.in_(ACTIVE),
                        attempts.c.lease_until > now,
                    )
                    .values(**values)
                    .returning(*attempts.c)
                )
            ).first()
        if row is None:
            raise OwnershipLost()
        return _attempt(row)

    async def finish(self, attempt_id, result, reason, now, job):
        state = "abandoned" if result == AttemptResult.ABANDONED else "finished"
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(attempts)
                    .where(attempts.c.id == attempt_id, attempts.c.state.in_(ACTIVE))
                    .values(state=state, result=result, reason=reason, finished_at=now)
                    .returning(attempts.c.job_id)
                )
            ).first()
            # Исход попытки записывается один раз: повтор события ничего не меняет
            if row is None:
                return False
            await connection.execute(
                update(jobs)
                .where(jobs.c.id == row.job_id)
                .values(**job, version=jobs.c.version + 1)
            )
            return True

    async def stale(self, now):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(attempts).where(attempts.c.state.in_(ACTIVE), attempts.c.lease_until <= now)
            )
            return [_attempt(row) for row in rows]

    async def recover(self, attempt_id, now):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(attempts)
                    .where(
                        attempts.c.id == attempt_id,
                        attempts.c.state.in_(ACTIVE),
                        attempts.c.lease_until <= now,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return
            attempt = _attempt(row)
            if attempt.state == "claimed":
                # Вызов ещё не отдавали шлюзу: попытку можно не засчитывать
                attempt_state = "abandoned"
                job = {
                    "state": "planned" if attempt.number == 1 else "waiting_retry",
                    "attempts": attempt.number - 1,
                }
            else:
                # Вызов мог состояться: вслепую не перезваниваем, решает человек
                attempt_state = "unknown"
                job = {"state": "manual", "reason": "unknown_" + attempt.state}
            await connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(state=attempt_state, finished_at=now)
            )
            await connection.execute(
                update(jobs)
                .where(jobs.c.id == attempt.job_id)
                .values(**job, version=jobs.c.version + 1)
            )

    async def contact(self, client_id):
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(select(contacts).where(contacts.c.client_id == client_id))
            ).first()
            return None if row is None else Contact.model_validate(row._asdict())

    async def save_contact(self, contact):
        data = contact.model_dump()
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(contacts)
                .values(**data)
                .on_conflict_do_update(
                    index_elements=[contacts.c.client_id], set_={"endpoint": data["endpoint"]}
                )
            )

    async def opt_out(self, client_id, now):
        async with self.engine.begin() as connection:
            await connection.execute(
                update(contacts)
                .where(and_(contacts.c.client_id == client_id, contacts.c.opt_out_at.is_(None)))
                .values(opt_out_at=now)
            )

    async def confirmed(self, visit_id):
        async with self.engine.connect() as connection:
            row = await connection.execute(
                select(confirmations.c.visit_id).where(confirmations.c.visit_id == visit_id)
            )
            return row.first() is not None

    async def mark_confirmed(self, visit_id, now):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(confirmations)
                .values(visit_id=visit_id, confirmed_at=now)
                .on_conflict_do_nothing()
            )
