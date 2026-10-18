"""T11: отбор С-10, попытки и исходы на эмуляторе МИС; вызов и диалог подменены."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from app.clock import CLINIC_TIMEZONE
from app.demo import demo_emulator
from app.mis.http import HttpMedicalSystem
from app.mis.models import Client, ClientComment, VisitChange, VisitCreate, VisitStatus
from app.reminders.calendar import ClinicCalendar
from app.reminders.executor import LEASE, ReminderExecutor
from app.reminders.marks import ReminderMarks, confirmation_line, confirmed_in
from app.reminders.memory import MemoryReminderStore
from app.reminders.models import Contact, ReminderJob
from app.reminders.planner import ReminderPlanner
from app.reminders.ports import DialRejected
from app.storage.reminders import PostgresReminderStore

PHONE = "PJSIP/phone-021@gateway"
CALENDAR = ClinicCalendar(frozenset({date(2026, 11, 4)}))


async def test_unknown_originate_expires_to_manual_instead_of_being_renewed(stand):
    await ready(stand)

    async def uncertain(*args):
        stand.dialer.calls.append(args)
        raise TimeoutError()

    stand.dialer.call_out = uncertain
    await stand.executor.tick()
    attempt_id = stand.dialer.calls[0][0]
    assert stand.executor.active is None
    stand.clock.value += timedelta(seconds=61)
    await stand.executor.tick()
    attempt = await stand.store.attempt(attempt_id)
    assert attempt.state == "unknown"
    assert (await stand.store.job(attempt.job_id)).state == "manual"
    assert len(stand.dialer.calls) == 1


async def test_friday_selection_reads_one_date_range():
    class Mis:
        def __init__(self):
            self.calls = []

        async def visits(self, **query):
            self.calls.append(query)
            return []

    mis = Mis()
    planner = ReminderPlanner(mis, MemoryReminderStore(), CALENDAR, Clock(at(16, 10)))
    assert await planner.plan(date(2026, 10, 16)) == 0
    assert len(mis.calls) == 1
    assert mis.calls[0]["date_from"] == date(2026, 10, 17)
    assert mis.calls[0]["date_to"] == date(2026, 10, 19)


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CLINIC_TIMEZONE)


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class FreeBudget:
    async def reserve(self, cost=1):
        return None

    async def release(self, ids):
        pass

    async def touch(self, place):
        return 0.0

    async def defer(self, seconds):
        pass


class Dialer:
    def __init__(self, reject: bool = False):
        self.calls = []
        self.reject = reject

    async def call_out(self, attempt_id, endpoint, context, channel_id):
        self.calls.append((attempt_id, endpoint, context, channel_id))
        if self.reject:
            raise DialRejected()


# 1. Календарь: за один рабочий день до приёма, в окна обзвона


def test_monday_visit_is_reminded_on_friday_and_holidays_are_skipped():
    assert CALENDAR.call_day(date(2026, 10, 19)) == date(2026, 10, 16)
    assert CALENDAR.call_day(date(2026, 10, 20)) == date(2026, 10, 19)
    # 4 ноября — праздник: о четверге напоминаем во вторник
    assert CALENDAR.call_day(date(2026, 11, 5)) == date(2026, 11, 3)
    assert CALENDAR.visit_days(date(2026, 10, 16)) == [
        date(2026, 10, 17),
        date(2026, 10, 18),
        date(2026, 10, 19),
    ]


@pytest.mark.parametrize(
    "moment, open_",
    [
        (at(19, 9, 59), False),
        (at(19, 10), True),
        (at(19, 18, 59), True),
        (at(19, 19), False),
        (at(17, 14, 59), True),
        (at(17, 15), False),
        (at(18, 12), False),
        (at(4, 12, month=11), False),
    ],
)
def test_call_windows_follow_clinic_time(moment, open_):
    assert CALENDAR.is_open(moment) is open_


def test_window_uses_clinic_timezone_not_the_computer():
    moscow = datetime(2026, 10, 19, 9, 30, tzinfo=CLINIC_TIMEZONE).astimezone(
        datetime(2026, 1, 1).astimezone().tzinfo
    )
    assert not CALENDAR.is_open(moscow)


def test_holidays_are_loaded_from_config():
    assert date(2026, 11, 4) in ClinicCalendar.load().holidays


# 2. Стенд: эмулятор с визитами, созданными в разное время


class Stand:
    def __init__(self, store=None):
        self.clock = Clock(at(18, 9))
        self.emulator = demo_emulator(self.clock)
        self.store = store or MemoryReminderStore()
        self.dialer = Dialer()
        self.http = httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=self.emulator.transport(),
        )
        self.mis = HttpMedicalSystem(self.http, FreeBudget())
        self.executor = self.make_executor("vm-1")

    def make_executor(self, owner):
        return ReminderExecutor(
            self.store,
            self.mis,
            CALENDAR,
            self.clock,
            self.dialer,
            owner=owner,
            allowed_endpoints={PHONE},
        )

    async def visit(self, client_id, scheduled_at, created_at=None):
        self.clock.value, saved = created_at or self.clock.value, self.clock.value
        visit = await self.mis.create_visit(
            VisitCreate(client_id=client_id, doctor_id=1, scheduled_at=scheduled_at)
        )
        self.clock.value = saved
        return visit

    async def contact(self, client_id, endpoint=PHONE):
        await self.store.save_contact(Contact(client_id=client_id, endpoint=endpoint))

    def comment(self, client_id, text):
        clients = self.emulator.clients["demo"]
        clients[client_id] = clients[client_id].model_copy(update={"comment": text})

    def gets(self, path):
        return sum(1 for _, method, p, _ in self.emulator.log if method == "GET" and p == path)

    async def job(self, visit_id):
        return next(j for j in await self.jobs() if j.visit_id == visit_id)

    async def jobs(self):
        if isinstance(self.store, MemoryReminderStore):
            return list(self.store.jobs.values())
        from sqlalchemy import select

        from app.storage.schema import reminder_jobs

        async with self.store.engine.connect() as connection:
            rows = await connection.execute(select(reminder_jobs))
            return [ReminderJob.model_validate(row._asdict()) for row in rows]


@pytest.fixture(params=["memory", "postgres"])
def reminder_store(request):
    # Одни и те же правила проверяем на памяти и на настоящем PostgreSQL
    if request.param == "memory":
        return MemoryReminderStore()
    return PostgresReminderStore(request.getfixturevalue("pg_engine"))


@pytest.fixture
async def stand(reminder_store):
    value = Stand(reminder_store)
    yield value
    await value.http.aclose()


async def test_planning_applies_regulation_filters_with_one_request(stand):
    # Визиты на вторник 20.10: звоним в понедельник 19.10
    old = await stand.visit(3, at(20, 10))
    no_phone = await stand.visit(1, at(20, 11))
    fresh = await stand.visit(2, at(20, 12), created_at=at(19, 9, 30))
    later = await stand.visit(3, at(20, 13), created_at=at(18, 12))
    cancelled = await stand.visit(3, at(20, 14))
    await stand.mis.change_visit(cancelled.id, VisitChange(status=VisitStatus.CANCELLED))
    for client in (2, 3):
        await stand.contact(client)
    stand.clock.value = at(19, 10, 5)
    before = stand.gets("visits")
    assert await stand.executor.planner.plan(date(2026, 10, 19)) == 2
    assert stand.gets("visits") - before == 1
    assert (await stand.job(old.id)).state == "planned"
    assert (await stand.job(no_phone.id)).reason == "no_contact"
    # Создана менее суток назад и до конца окна сутки не исполнятся
    assert (await stand.job(fresh.id)).reason == "created_recently"
    # Сутки исполнятся в 12:00 — тогда и позвоним
    assert (await stand.job(later.id)).next_at == at(19, 12)
    assert all(j.visit_id != cancelled.id for j in await stand.jobs())
    # Повторный отбор в тот же день не читает МИС
    assert await stand.executor.planner.plan(date(2026, 10, 19)) == 0
    assert stand.gets("visits") - before == 1


async def ready(stand, *, comment=None, endpoint=PHONE, now=None):
    visit = await stand.visit(3, at(20, 10))
    await stand.contact(3, endpoint)
    if comment:
        stand.comment(3, comment)
    stand.clock.value = now or at(19, 10, 5)
    return visit


async def test_call_goes_out_once_with_only_first_name_and_patronymic(stand):
    visit = await ready(stand)
    await stand.executor.tick()
    [(attempt_id, endpoint, context, channel_id)] = stand.dialer.calls
    assert endpoint == PHONE and context.visit.visit_id == visit.id
    assert context.addressee == "Иван Иванович"
    attempt = await stand.store.attempt(attempt_id)
    assert attempt.state == "dialing" and context.fence == attempt.fence == 1
    # Канал задан заранее и связан с попыткой: после сбоя звонок можно найти в АТС
    assert channel_id == attempt.channel_id == f"reminder-{attempt_id}"
    assert await stand.executor.guard(attempt_id, 1)
    assert not await stand.executor.guard(attempt_id, 2)
    await stand.executor.tick()
    assert len(stand.dialer.calls) == 1


async def test_outcome_is_written_once(stand):
    visit = await ready(stand)
    await stand.executor.tick()
    attempt_id = stand.dialer.calls[0][0]
    assert await stand.executor.finished(attempt_id, "CONFIRMED")
    assert not await stand.executor.finished(attempt_id, "CANCELLED")
    job = await stand.job(visit.id)
    assert (job.state, job.outcome, job.sms_pending) == ("done", "CONFIRMED", False)


async def test_no_answer_retries_after_two_hours_then_needs_sms(stand):
    visit = await ready(stand)
    await stand.executor.tick()
    await stand.executor.finished(stand.dialer.calls[0][0], "NO_ANSWER", "q850_19")
    job = await stand.job(visit.id)
    assert (job.state, job.next_at) == ("waiting_retry", at(19, 12, 5))
    stand.clock.value = at(19, 12, 4)
    await stand.executor.tick()
    assert len(stand.dialer.calls) == 1
    stand.clock.value = at(19, 12, 5)
    await stand.executor.tick()
    assert len(stand.dialer.calls) == 2
    second = stand.dialer.calls[1][0]
    assert (await stand.store.attempt(second)).number == 2
    await stand.executor.finished(second, "NO_ANSWER")
    job = await stand.job(visit.id)
    assert (job.state, job.outcome, job.sms_pending) == ("done", "NO_ANSWER", True)
    stand.clock.value = at(19, 16)
    await stand.executor.tick()
    assert len(stand.dialer.calls) == 2


async def test_retry_that_does_not_fit_the_window_becomes_sms(stand):
    visit = await ready(stand, now=at(19, 17, 30))
    await stand.executor.tick()
    await stand.executor.finished(stand.dialer.calls[0][0], "FAILED", "originate_rejected")
    job = await stand.job(visit.id)
    assert (job.state, job.outcome, job.sms_pending) == ("done", "NO_ANSWER", True)


async def test_wrong_person_is_final_and_needs_sms(stand):
    visit = await ready(stand)
    await stand.executor.tick()
    await stand.executor.finished(stand.dialer.calls[0][0], "WRONG_PERSON")
    job = await stand.job(visit.id)
    assert (job.state, job.sms_pending) == ("done", True)


async def test_visit_changed_before_the_call_is_not_called(stand):
    visit = await ready(stand)
    await stand.executor.planner.plan(date(2026, 10, 19))
    await stand.mis.change_visit(visit.id, VisitChange(status=VisitStatus.CANCELLED))
    await stand.executor.tick()
    assert not stand.dialer.calls
    assert (await stand.job(visit.id)).reason == "changed"


@pytest.mark.parametrize(
    "comment, reason",
    [
        ("Аллергия\nПодтверждена запись #{id} 20.10.2026 10:00", "already_confirmed"),
        ("Аллергия\nОтказ от обзвона 01.10.2026", "opt_out"),
    ],
)
async def test_marks_in_the_client_comment_stop_the_call(stand, comment, reason):
    visit = await ready(stand)
    stand.comment(3, comment.format(id=visit.id))
    await stand.executor.tick()
    assert not stand.dialer.calls
    assert (await stand.job(visit.id)).reason == reason


async def test_only_the_team_softphone_can_be_called(stand):
    visit = await ready(stand, endpoint="PJSIP/+79990000000@gateway")
    await stand.executor.tick()
    assert not stand.dialer.calls
    assert (await stand.job(visit.id)).reason == "endpoint_not_allowed"


async def test_no_calls_outside_the_window(stand):
    await ready(stand, now=at(19, 19, 30))
    await stand.executor.tick()
    assert not stand.dialer.calls and not await stand.jobs()


async def test_rejected_originate_is_a_failed_attempt(stand):
    visit = await ready(stand)
    stand.dialer.reject = True
    await stand.executor.tick()
    job = await stand.job(visit.id)
    assert (job.state, job.reason) == ("waiting_retry", "originate_rejected")
    assert stand.executor.active is None


# 3. Перезапуск: неизвестный исход не повторяется вслепую


async def test_restart_after_dialing_goes_to_manual_without_redial(stand):
    visit = await ready(stand)
    await stand.executor.tick()
    attempt_id = stand.dialer.calls[0][0]
    restarted = stand.make_executor("vm-2")
    stand.clock.value += LEASE + timedelta(seconds=1)
    await restarted.tick()
    assert (await stand.store.attempt(attempt_id)).state == "unknown"
    job = await stand.job(visit.id)
    assert (job.state, job.reason) == ("manual", "unknown_dialing")
    assert len(stand.dialer.calls) == 1
    # Поздний исход от прежнего процесса уже ничего не меняет
    assert not await stand.executor.finished(attempt_id, "CONFIRMED")


async def test_restart_before_dialing_releases_the_attempt(stand):
    visit = await ready(stand)
    await stand.executor.planner.plan(date(2026, 10, 19))
    job = await stand.job(visit.id)
    attempt = await stand.store.claim(job.id, "vm-1", stand.clock.now(), LEASE)
    stand.clock.value += LEASE + timedelta(seconds=1)
    await stand.make_executor("vm-2").tick()
    assert (await stand.store.attempt(attempt.id)).state == "abandoned"
    [(second, _, _, _)] = stand.dialer.calls
    assert (await stand.store.attempt(second)).number == 1


async def test_lease_is_renewed_while_the_call_lasts(stand):
    await ready(stand)
    await stand.executor.tick()
    attempt_id = stand.dialer.calls[0][0]
    for _ in range(10):
        stand.clock.value += timedelta(seconds=30)
        await stand.executor.tick()
    assert (await stand.store.attempt(attempt_id)).state == "dialing"


# 4. Отметки в комментарии клиента: журнал, свежий визит, без повторного PATCH


async def calling(stand, **kwargs):
    """Звонок идёт: у попытки действующий lease, контекст как у диалога."""
    visit = await ready(stand, **kwargs)
    await stand.executor.tick()
    context = stand.dialer.calls[0][2]
    return visit, context, ReminderMarks(stand.mis, stand.store, stand.clock, owner="vm-1")


def comment(stand):
    return stand.emulator.clients["demo"][3].comment


async def test_confirmation_keeps_the_previous_text_verbatim(stand):
    visit, context, marks = await calling(stand, comment="Аллергия на пенициллин  \n\n")
    assert await marks.confirm(context) == "applied"
    line = confirmation_line(visit.id, visit.scheduled_at)
    assert comment(stand) == "Аллергия на пенициллин  \n\n" + line
    assert line == f"Подтверждена запись #{visit.id} 20.10.2026 10:00"
    # Повтор доставки результата той же попытки — та же запись журнала, второго PATCH нет
    assert await marks.confirm(context) == "applied"
    assert stand.emulator.patch_count == 1 and await stand.store.confirmed(visit.id)


async def test_confirmation_that_does_not_fit_is_manual_and_truncates_nothing(stand):
    visit, context, marks = await calling(stand, comment="х" * 990)
    assert await marks.confirm(context) == "manual"
    assert comment(stand) == "х" * 990 and stand.emulator.patch_count == 0
    assert (await stand.store.mark(f"confirm:{context.attempt_id}")).reason == "comment_too_long"
    assert await stand.store.confirmed(visit.id)


async def test_lost_patch_response_is_settled_by_reading_only(stand):
    visit, context, marks = await calling(stand)
    stand.emulator.next_failure = "lost_response"
    assert await marks.confirm(context) == "pending"
    stand.clock.value += timedelta(minutes=1)
    assert await marks.reconcile() == 1
    assert (await stand.store.mark(f"confirm:{context.attempt_id}")).state == "applied"
    assert stand.emulator.patch_count == 1


async def test_unknown_patch_is_never_sent_again_and_ends_in_manual(stand):
    from app.mis.ports import MisOutcomeUnknown

    visit, context, marks = await calling(stand)
    sent = []

    async def lost(client_id, request):
        sent.append(request)
        raise MisOutcomeUnknown("TRANSPORT_FAILURE")

    stand.mis.update_client_comment = lost
    assert await marks.confirm(context) == "pending"
    for _ in range(3):
        stand.clock.value += timedelta(minutes=1)
        await marks.reconcile()
    mark = await stand.store.mark(f"confirm:{context.attempt_id}")
    assert (mark.state, mark.reason) == ("manual", "not_visible")
    # И повторная доставка результата той же попытки не отправляет PATCH снова
    assert await marks.confirm(context) == "manual"
    assert len(sent) == 1


async def test_visit_changed_after_the_call_started_is_not_confirmed(stand):
    visit, context, marks = await calling(stand)
    await stand.mis.change_visit(visit.id, VisitChange(scheduled_at=at(21, 10)))
    assert await marks.confirm(context) == "manual"
    assert stand.emulator.patch_count == 1  # только сам перенос визита
    assert (await stand.store.mark(f"confirm:{context.attempt_id}")).reason == "visit_changed"


async def test_stale_attempt_cannot_write_the_comment(stand):
    visit, context, marks = await calling(stand)
    await stand.executor.finished(context.attempt_id, "INCOMPLETE")
    assert await marks.confirm(context) == "manual"
    assert stand.emulator.patch_count == 0
    reason = (await stand.store.mark(f"confirm:{context.attempt_id}")).reason
    assert reason == "attempt_not_current"


async def test_opt_out_is_local_at_once_and_marked_in_the_comment(stand):
    visit, context, marks = await calling(stand, comment="Аллергия")
    assert await marks.opt_out(context) == "applied"
    assert comment(stand) == "Аллергия\nОтказ от обзвона 19.10.2026"
    assert (await stand.store.contact(3)).opt_out_at == stand.clock.now()


async def test_concurrent_marks_of_one_patient_do_not_lose_each_other(stand):
    import asyncio

    visit, context, marks = await calling(stand, comment="Аллергия")
    other = ReminderMarks(stand.mis, stand.store, stand.clock, owner="vm-2")
    results = await asyncio.gather(marks.confirm(context), other.opt_out(context))
    assert results == ["applied", "applied"]
    text = comment(stand)
    assert text.startswith("Аллергия\n")
    assert confirmation_line(visit.id, visit.scheduled_at) in text
    assert "Отказ от обзвона 19.10.2026" in text


def test_confirmation_match_is_by_whole_line_with_time():
    when = at(20, 10)
    assert not confirmed_in("Подтверждена запись #10 20.10.2026 10:00", 1, when)
    assert not confirmed_in("Подтверждена запись #1 21.10.2026 10:00", 1, when)
    assert confirmed_in("Аллергия\nПодтверждена запись #1 20.10.2026 10:00", 1, when)


def test_calendar_outside_the_checked_period_is_not_a_working_window():
    calendar = ClinicCalendar(frozenset(), (date(2026, 10, 1), date(2026, 12, 31)))
    assert calendar.is_open(at(19, 12))
    assert not calendar.is_open(datetime(2027, 1, 11, 12, tzinfo=CLINIC_TIMEZONE))


async def test_client_key_fields_cannot_be_patched_on_the_emulator(stand):
    response = await stand.http.patch("clients/3", json={"last_name": "Другой"})
    assert response.status_code == 400
    with pytest.raises(ValueError):
        ClientComment(comment="a\x0bb").body()
    assert isinstance(await stand.mis.client(3), Client)


async def test_mis_failure_before_the_call_does_not_use_an_attempt(stand):
    from app.mis.ports import MisUnavailable

    visit = await ready(stand)
    original = stand.mis.visit

    async def unavailable(visit_id):
        raise MisUnavailable("SERVER_FAILURE")

    stand.mis.visit = unavailable
    await stand.executor.tick()
    job = await stand.job(visit.id)
    assert (job.state, job.attempts, job.reason) == ("planned", 0, "SERVER_FAILURE")
    assert not stand.dialer.calls
    stand.mis.visit = original
    stand.clock.value += timedelta(minutes=5)
    await stand.executor.tick()
    [(attempt_id, _, _, _)] = stand.dialer.calls
    assert (await stand.store.attempt(attempt_id)).number == 1
