"""Настоящий диалог + настоящий журнал отметки + HTTP-эмулятор, без фейка диалога."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fixture_bridge import pipeline, turn
from test_rescheduling import SETUP

from app.cancellation.models import summary
from app.dialogue.outbound import OutboundContext
from app.dialogue.reminder import ReminderConversation
from app.reminders.calendar import ClinicCalendar
from app.reminders.dialogue_adapter import ReminderActionAdapter
from app.reminders.executor import ReminderExecutor
from app.reminders.marks import ReminderMarks
from app.reminders.memory import MemoryReminderStore
from app.reminders.models import Contact, ReminderJob, ReminderMark
from app.storage.reminders import PostgresReminderStore


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def integration_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@asynccontextmanager
async def integrated(engine):
    async with pipeline(SETUP, pg_engine=engine) as parts:
        client, service, emulator, clock, *_ = parts
        store = (
            PostgresReminderStore(engine)
            if engine is not None
            else MemoryReminderStore(service.store)
        )
        visit = emulator.records["demo"][1]
        old = emulator.clients["demo"][visit.client_id]
        emulator.clients["demo"][visit.client_id] = old.model_copy(
            update={"comment": "Чужой текст  \n"}
        )
        await store.save_contact(
            Contact(client_id=visit.client_id, endpoint="PJSIP/phone-021@gateway")
        )
        job = ReminderJob(
            visit_id=visit.id,
            client_id=visit.client_id,
            doctor_id=visit.doctor.id,
            scheduled_at=visit.scheduled_at,
            call_day=clock.now().date(),
            next_at=clock.now(),
        )
        await store.add_job(job)
        attempt = await store.claim(job.id, "test", clock.now(), timedelta(minutes=3))
        await store.advance(attempt.id, "test", "answered", clock.now(), timedelta(minutes=3))
        marks = ReminderMarks(
            service.booking.mis, store, clock, owner="test", require_dialogue_guard=True
        )
        executor = ReminderExecutor(
            store,
            service.booking.mis,
            ClinicCalendar(),
            clock,
            None,
            owner="test",
            allowed_endpoints={"PJSIP/phone-021@gateway"},
            marks=marks,
        )
        actions = ReminderActionAdapter(executor, marks, store, clock)
        service.reminders = ReminderConversation(service.conversation, actions)
        context = OutboundContext(
            attempt_id=attempt.id,
            fence=attempt.fence,
            client_id=visit.client_id,
            visit=summary(visit),
            addressee="Ольга Викторовна",
        )
        await service.open_outbound("scenario", context)
        await turn(client, 2, "Да")
        yield client, service, emulator, clock, store, marks, executor, context


@pytest.mark.parametrize(
    "text,kind,outcome",
    [("Да, приду", "confirm", "CONFIRMED"), ("Не звоните мне", "opt_out", "OPT_OUT")],
)
async def test_actual_dialogue_writes_a_preserved_comment_once(
    integration_engine, text, kind, outcome
):
    async with integrated(integration_engine) as (client, _, emulator, _, store, _, _, context):
        result = await turn(client, 3, text)
        assert result["reminder_result"]["outcome"] == outcome
        assert result["reminder_result"]["mis_status"] == "applied", result
        comment = emulator.clients["demo"][context.client_id].comment
        assert comment.startswith("Чужой текст  \n")
        assert (await store.mark(f"{kind}:{context.attempt_id}")).state == "applied"
        assert emulator.patch_count == 1
        assert result == await turn(client, 3, text)
        assert emulator.patch_count == 1


async def test_input_epoch_is_checked_in_the_sending_transaction(integration_engine):
    async with integrated(integration_engine) as (
        client,
        service,
        emulator,
        _,
        store,
        _,
        _,
        context,
    ):

        async def interrupt(request):
            if request.method == "GET" and "/clients/" in request.url.path:
                await service.input_started("scenario", 4)

        service.booking.mis._client.event_hooks["request"].append(interrupt)
        result = await turn(client, 3, "Да, приду")
        assert result["code"] == "TURN_INTERRUPTED"
        assert emulator.patch_count == 0
        assert (await store.mark(f"confirm:{context.attempt_id}")).state == "prepared"


async def test_unknown_patch_and_restart_sending_are_read_only(integration_engine):
    async with integrated(integration_engine) as (
        client,
        _,
        emulator,
        clock,
        store,
        marks,
        executor,
        context,
    ):
        emulator.next_failure = "lost_response"
        result = await turn(client, 3, "Да, приду")
        assert result["reminder_result"]["mis_status"] == "pending"
        mark_id = f"confirm:{context.attempt_id}"
        assert (await store.mark(mark_id)).state == "unknown"
        await executor.finished(context.attempt_id, "CONFIRMED", mis_status="pending")
        clock.value += timedelta(minutes=1)
        await marks.reconcile()
        assert (await store.mark(mark_id)).state == "applied"
        attempt = await store.attempt(context.attempt_id)
        assert (await store.job(attempt.job_id)).mis_status == "applied"
        assert emulator.patch_count == 1
        orphan = ReminderMark(
            id="confirm:orphan",
            attempt_id="orphan",
            kind="confirm",
            client_id=context.client_id,
            visit_id=context.visit.visit_id,
            line="Подтверждена запись #999 01.10.2026 09:00",
            updated_at=clock.now(),
        )
        await store.prepare_mark(orphan)
        await store.move_mark(orphan.id, {"prepared"}, state="sending", updated_at=clock.now())
        clock.value += timedelta(minutes=3)
        await marks.reconcile()
        assert (await store.mark(orphan.id)).state == "unknown"
        assert emulator.patch_count == 1
        assert not await store.lock_client(
            context.client_id, "other", clock.now(), timedelta(minutes=1)
        )


async def test_confirmation_is_specific_to_the_visit_fingerprint(integration_engine):
    async with integrated(integration_engine) as (_, _, _, clock, store, _, _, context):
        v = context.visit
        await store.mark_confirmed(v.visit_id, clock.now(), v.scheduled_at, v.doctor_id)
        assert await store.confirmed(v.visit_id, v.scheduled_at, v.doctor_id)
        assert not await store.confirmed(
            v.visit_id, v.scheduled_at + timedelta(days=1), v.doctor_id
        )
        assert not await store.confirmed(v.visit_id, v.scheduled_at, v.doctor_id + 1)


async def test_interrupted_answered_call_cannot_schedule_a_second_call(integration_engine):
    async with integrated(integration_engine) as (_, _, _, _, store, _, executor, context):
        await executor.finished(context.attempt_id, "INCOMPLETE")
        attempt = await store.attempt(context.attempt_id)
        job = await store.job(attempt.job_id)
        assert job.state == "manual" and job.outcome == "ESCALATED"


async def test_both_same_process_writers_do_not_share_a_reentrant_lock(integration_engine):
    async with integrated(integration_engine) as (_, _, _, clock, store, _, _, context):
        assert await store.lock_client(
            context.client_id, "operation-1", clock.now(), timedelta(minutes=1)
        )
        assert not await store.lock_client(
            context.client_id, "operation-2", clock.now(), timedelta(minutes=1)
        )
