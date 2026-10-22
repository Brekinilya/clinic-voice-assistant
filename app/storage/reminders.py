"""Задания, попытки и журнал отметок напоминаний в PostgreSQL: общие для процессов.

Каждый метод — одна короткая транзакция; во время HTTP транзакция не держится. Гонку двух
исполнителей за одну попытку решают уникальные индексы: проигравший получает None.
"""

from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.dialogue.reminder_guard import check_reminder_guard
from app.outbox.models import OutboxJob, reminder_sms_event
from app.recovery.models import OwnershipLost
from app.reminders.memory import channel_for
from app.reminders.models import (
    AttemptResult,
    Contact,
    ReminderAttempt,
    ReminderJob,
    ReminderMark,
)
from app.storage.outbox import decode as decode_outbox
from app.storage.outbox import values as outbox_values
from app.storage.schema import dialogue_sessions
from app.storage.schema import outbox_jobs as event_jobs
from app.storage.schema import reminder_attempts as attempts
from app.storage.schema import reminder_client_locks as locks
from app.storage.schema import reminder_confirmations as confirmations
from app.storage.schema import reminder_contacts as contacts
from app.storage.schema import reminder_days as days
from app.storage.schema import reminder_jobs as jobs
from app.storage.schema import reminder_marks as marks

ACTIVE = ("claimed", "dialing", "answered")


def _job(row) -> ReminderJob:
    return ReminderJob.model_validate(row._asdict())


def _attempt(row) -> ReminderAttempt:
    return ReminderAttempt.model_validate(row._asdict())


def _mark(row) -> ReminderMark:
    return ReminderMark.model_validate(row._asdict())


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

    async def add_job(self, job, now=None):
        async with self.engine.begin() as connection:
            result = await connection.execute(
                insert(jobs).values(**job.model_dump()).on_conflict_do_nothing()
            )
            if result.rowcount == 1 and job.sms_pending:
                event = reminder_sms_event(
                    job.id, job.client_id, job.scheduled_at, job.room, now or job.next_at
                )
                await connection.execute(
                    insert(event_jobs)
                    .values(**outbox_values(event))
                    .on_conflict_do_nothing(
                        index_elements=[event_jobs.c.source_id, event_jobs.c.kind]
                    )
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
                fence = (
                    await connection.execute(
                        select(func.coalesce(func.max(attempts.c.fence), 0)).where(
                            attempts.c.job_id == job_id
                        )
                    )
                ).scalar_one() + 1
                number = job.attempts + 1
                attempt = ReminderAttempt(
                    job_id=job_id,
                    number=number,
                    fence=fence,
                    owner=owner,
                    lease_until=now + lease,
                    channel_id="",
                    started_at=now,
                )
                attempt = attempt.model_copy(update={"channel_id": channel_for(attempt.id)})
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

    async def guard(self, attempt_id, fence, now):
        async with self.engine.connect() as connection:
            row = await connection.execute(
                select(attempts.c.id).where(
                    attempts.c.id == attempt_id,
                    attempts.c.fence == fence,
                    attempts.c.state.in_(ACTIVE),
                    attempts.c.lease_until > now,
                )
            )
            return row.first() is not None

    async def finish(self, attempt_id, result, reason, now, job, mis_status=None):
        state = "abandoned" if result == AttemptResult.ABANDONED else "finished"
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(attempts)
                    .where(attempts.c.id == attempt_id, attempts.c.state.in_(ACTIVE))
                    .values(
                        state=state,
                        result=result,
                        mis_status=mis_status,
                        reason=reason,
                        finished_at=now,
                    )
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
            if job.get("sms_pending"):
                reminder = (
                    await connection.execute(select(jobs).where(jobs.c.id == row.job_id))
                ).first()
                if reminder is None:
                    raise RuntimeError("напоминание исчезло при завершении попытки")
                event = reminder_sms_event(
                    reminder.id, reminder.client_id, reminder.scheduled_at, reminder.room, now
                )
                await connection.execute(
                    insert(event_jobs)
                    .values(**outbox_values(event))
                    .on_conflict_do_nothing(
                        index_elements=[event_jobs.c.source_id, event_jobs.c.kind]
                    )
                )
            return True

    async def outbox_events(self, source_id: str) -> tuple[OutboxJob, ...]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(event_jobs).where(
                        event_jobs.c.source_id == source_id,
                        event_jobs.c.kind == "reminder_sms",
                    )
                )
            ).all()
        return tuple(decode_outbox(row) for row in rows)

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
            # Смена контакта не снимает ранее сохранённые отказы.
            await connection.execute(
                insert(contacts)
                .values(**data)
                .on_conflict_do_update(
                    index_elements=[contacts.c.client_id],
                    set_={
                        "endpoint": data["endpoint"],
                        "opt_out_at": func.coalesce(contacts.c.opt_out_at, data["opt_out_at"]),
                        "notification_opt_out_at": func.coalesce(
                            contacts.c.notification_opt_out_at,
                            data["notification_opt_out_at"],
                        ),
                    },
                )
            )

    async def opt_out(self, client_id, now):
        async with self.engine.begin() as connection:
            await connection.execute(
                update(contacts)
                .where(and_(contacts.c.client_id == client_id, contacts.c.opt_out_at.is_(None)))
                .values(opt_out_at=now)
            )

    async def opt_out_all(self, client_id, now):
        async with self.engine.begin() as connection:
            await connection.execute(
                update(contacts)
                .where(contacts.c.client_id == client_id)
                .values(
                    opt_out_at=func.coalesce(contacts.c.opt_out_at, now),
                    notification_opt_out_at=func.coalesce(contacts.c.notification_opt_out_at, now),
                )
            )
            await connection.execute(
                update(event_jobs)
                .where(
                    event_jobs.c.kind == "reminder_sms",
                    event_jobs.c.state.in_(["pending", "retry"]),
                    event_jobs.c.payload["client_id"].astext == str(client_id),
                )
                .values(state="blocked", last_error="NOTIFICATION_OPT_OUT")
            )

    async def confirmed(self, visit_id, scheduled_at=None, doctor_id=None):
        async with self.engine.connect() as connection:
            query = select(confirmations.c.visit_id).where(confirmations.c.visit_id == visit_id)
            if scheduled_at is not None:
                query = query.where(
                    confirmations.c.scheduled_at == scheduled_at,
                    confirmations.c.doctor_id == doctor_id,
                )
            row = await connection.execute(query.limit(1))
            return row.first() is not None

    async def mark_confirmed(self, visit_id, now, scheduled_at=None, doctor_id=None):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(confirmations)
                .values(
                    visit_id=visit_id,
                    confirmed_at=now,
                    scheduled_at=scheduled_at or now,
                    doctor_id=doctor_id or 0,
                )
                .on_conflict_do_nothing()
            )

    async def prepare_mark(self, mark):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(marks).values(**mark.model_dump()).on_conflict_do_nothing()
            )
            return _mark(
                (await connection.execute(select(marks).where(marks.c.id == mark.id))).one()
            )

    async def mark(self, mark_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(marks).where(marks.c.id == mark_id))).first()
            return None if row is None else _mark(row)

    async def start_mark(self, mark_id, context, now, owner, approval=None):
        async with self.engine.begin() as connection:
            if approval is not None:
                state = (
                    await connection.execute(
                        select(dialogue_sessions.c.state)
                        .where(dialogue_sessions.c.call_id == approval.call_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                check_reminder_guard(state, approval, context, mark_id.split(":", 1)[0])
            attempt = (
                await connection.execute(
                    select(attempts).where(attempts.c.id == context.attempt_id).with_for_update()
                )
            ).first()
            lock = (
                await connection.execute(
                    select(locks).where(locks.c.client_id == context.client_id).with_for_update()
                )
            ).first()
            unresolved = await connection.scalar(
                select(marks.c.id)
                .where(
                    marks.c.client_id == context.client_id,
                    marks.c.id != mark_id,
                    or_(
                        marks.c.state.in_(["sending", "unknown"]),
                        and_(
                            marks.c.state == "manual",
                            marks.c.reason.in_(
                                ["not_visible", "sending_interrupted", "not_in_response"]
                            ),
                        ),
                    ),
                )
                .limit(1)
            )
            if unresolved is not None:
                raise OwnershipLost()
            if (
                attempt is None
                or attempt.fence != context.fence
                or attempt.state not in ACTIVE
                or attempt.lease_until <= now
                or lock is None
                or lock.owner != owner
                or lock.until <= now
            ):
                raise OwnershipLost()
            row = (
                await connection.execute(
                    update(marks)
                    .where(marks.c.id == mark_id, marks.c.state == "prepared")
                    .values(state="sending", updated_at=now)
                    .returning(*marks.c)
                )
            ).first()
            return None if row is None else _mark(row)

    async def move_mark(self, mark_id, expected, **changes):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(marks)
                    .where(marks.c.id == mark_id, marks.c.state.in_(sorted(expected)))
                    .values(**changes)
                    .returning(*marks.c)
                )
            ).first()
            if row is not None and row.state in {"applied", "manual"}:
                attempt = (
                    await connection.execute(
                        select(attempts).where(attempts.c.id == row.attempt_id)
                    )
                ).first()
                if attempt is not None:
                    await connection.execute(
                        update(attempts)
                        .where(attempts.c.id == attempt.id, attempts.c.mis_status == "pending")
                        .values(mis_status=row.state)
                    )
                    await connection.execute(
                        update(jobs)
                        .where(
                            jobs.c.id == attempt.job_id,
                            jobs.c.mis_status == "pending",
                            jobs.c.outcome.in_(["CONFIRMED", "OPT_OUT"]),
                        )
                        .values(
                            mis_status=row.state,
                            state="done" if row.state == "applied" else "manual",
                        )
                    )
            return None if row is None else _mark(row)

    async def marks_to_check(self, now):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(marks).where(
                    or_(
                        and_(marks.c.state == "unknown", marks.c.next_check_at <= now),
                        and_(
                            marks.c.state == "sending",
                            marks.c.updated_at <= now - timedelta(minutes=2),
                        ),
                    )
                )
            )
            return [_mark(row) for row in rows]

    async def lock_client(self, client_id, owner, now, ttl):
        async with self.engine.begin() as connection:
            # Не начинаем новый PATCH поверх неизвестного прежнего, даже после истечения TTL.
            unresolved = await connection.scalar(
                select(marks.c.id)
                .where(
                    marks.c.client_id == client_id,
                    or_(
                        marks.c.state.in_(["sending", "unknown"]),
                        and_(
                            marks.c.state == "manual",
                            marks.c.reason.in_(
                                ["not_visible", "sending_interrupted", "not_in_response"]
                            ),
                        ),
                    ),
                )
                .limit(1)
            )
            if unresolved is not None:
                return False
            row = (
                await connection.execute(
                    insert(locks)
                    .values(client_id=client_id, owner=owner, until=now + ttl)
                    .on_conflict_do_update(
                        index_elements=[locks.c.client_id],
                        set_={"owner": owner, "until": now + ttl},
                        where=or_(locks.c.owner == owner, locks.c.until <= now),
                    )
                    .returning(locks.c.owner)
                )
            ).first()
            return row is not None and row.owner == owner

    async def unlock_client(self, client_id, owner):
        async with self.engine.begin() as connection:
            await connection.execute(
                locks.delete().where(locks.c.client_id == client_id, locks.c.owner == owner)
            )
