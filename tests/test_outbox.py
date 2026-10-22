"""Одинаковые гарантии очереди в памяти и PostgreSQL, включая неизвестный результат."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.booking.models import Approval
from app.cancellation.memory import MemoryCancellationStore
from app.cancellation.models import CancelState
from app.demo import DemoClock, demo_emulator
from app.mis.models import Client, Visit, VisitStatus
from app.outbox.delivery import DisabledDelivery, MemoryJournalDelivery, journal_receipt
from app.outbox.memory import MemoryOutboxStore
from app.outbox.models import (
    EventKind,
    JobState,
    OutboxError,
    appointment_sms_event,
    cancellation_events,
    prepare_comment_event,
    waitlist_sms_event,
)
from app.outbox.ports import (
    DeliveryUnknown,
    PermanentDeliveryFailure,
    TemporaryDeliveryFailure,
)
from app.outbox.worker import OutboxWorker
from app.storage.cancellation import PostgresCancellationStore
from app.storage.memory_ledger import MemoryLedger
from app.storage.outbox import PostgresOutboxStore
from app.storage.outbox_delivery import PostgresClientCommentDelivery, PostgresJournalDelivery
from app.storage.schema import delivery_journal


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def outbox_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@dataclass
class Context:
    store: object
    delivery: object
    cancellation: object
    action: object
    clock: DemoClock
    ledger: MemoryLedger | None
    engine: object


@pytest.fixture
async def outbox_context(outbox_engine):
    clock = DemoClock()
    before = Visit(
        id=1,
        client_id=1,
        doctor=demo_emulator(clock).doctors[1],
        scheduled_at=clock.now() + timedelta(days=1),
        status=VisitStatus.PLANNED,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    ledger = MemoryLedger() if outbox_engine is None else None
    if ledger is not None:
        cancellation = MemoryCancellationStore(ledger)
        store, delivery = MemoryOutboxStore(ledger), MemoryJournalDelivery(ledger, clock)
    else:
        cancellation = PostgresCancellationStore(outbox_engine)
        store, delivery = (
            PostgresOutboxStore(outbox_engine),
            PostgresJournalDelivery(outbox_engine, clock),
        )
    action = await cancellation.prepare("outbox-call", before, clock.now())
    await cancellation.claim(
        Approval(
            call_id=action.call_id,
            action_id=action.action_id,
            revision=action.revision,
            turn_id=1,
            is_final=True,
            permitted=True,
        ),
        clock.now(),
    )
    return Context(store, delivery, cancellation, action, clock, ledger, outbox_engine)


async def enqueue(ctx):
    await ctx.cancellation.finish(ctx.action.action_id, CancelState.CANCELLED, ctx.clock.now())
    return await ctx.store.list()


async def entries(ctx):
    if ctx.ledger is not None:
        return tuple(entry.model_dump(mode="json") for entry in ctx.ledger.deliveries.values())
    async with ctx.engine.connect() as connection:
        return tuple((await connection.execute(select(delivery_journal.c.entry))).scalars())


async def test_only_confirmed_cancel_enqueues_two_events_once(outbox_context):
    ctx = outbox_context
    assert await ctx.store.list() == ()
    assert await ctx.store.claim(ctx.clock.now()) is None
    await asyncio.gather(*(enqueue(ctx) for _ in range(5)))
    jobs = await ctx.store.list()
    assert len(jobs) == 2 and {j.payload.kind for j in jobs} == {
        EventKind.CANCELLATION_SMS,
        EventKind.SLOT_RELEASED,
    }
    assert all(j.source_id == ctx.action.action_id and j.state == JobState.PENDING for j in jobs)
    await ctx.cancellation.release_call(ctx.action.call_id)
    reopened = MemoryOutboxStore(ctx.ledger) if ctx.ledger else PostgresOutboxStore(ctx.engine)
    assert await reopened.list() == jobs
    assert await entries(ctx) == ()


async def test_unknown_cancel_does_not_enqueue_until_reconciliation(outbox_context):
    ctx = outbox_context
    await ctx.cancellation.finish(ctx.action.action_id, CancelState.RECONCILING, ctx.clock.now())
    assert await ctx.store.list() == ()
    await enqueue(ctx)
    assert len(await ctx.store.list()) == 2


async def test_concurrent_claims_have_one_owner_per_job(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    claims = await asyncio.gather(*(ctx.store.claim(ctx.clock.now()) for _ in range(8)))
    claimed = [j for j in claims if j is not None]
    assert len(claimed) == 2 and len({j.event_id for j in claimed}) == 2
    assert len({j.lease_token for j in claimed}) == 2
    assert all(j.attempts == 1 for j in claimed)


async def test_expired_lease_is_unknown_and_old_owner_cannot_complete(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    claims = [await ctx.store.claim(ctx.clock.now(), 1) for _ in range(2)]
    ctx.clock.value += timedelta(seconds=1)
    assert await ctx.store.claim(ctx.clock.now()) is None
    for job in claims:
        current = await ctx.store.get(job.event_id)
        assert current.state == JobState.UNKNOWN and current.last_error == "WORKER_LOST"
        with pytest.raises(OutboxError, match="LEASE_LOST"):
            await ctx.store.finish(
                job.event_id,
                job.lease_token,
                JobState.ACKNOWLEDGED,
                ctx.clock.now(),
                receipt=journal_receipt(job.event_id),
            )
        with pytest.raises(OutboxError, match="JOB_NOT_BLOCKED"):
            await ctx.store.resume_blocked(job.event_id, ctx.clock.now())


async def test_disabled_delivery_blocks_until_explicit_resume(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    worker = OutboxWorker(ctx.store, DisabledDelivery(), ctx.clock)
    for _ in range(2):
        assert (await worker.run_once()).state == JobState.BLOCKED
    assert await worker.run_once() is None and await entries(ctx) == ()
    assert len(await ctx.store.list(state=JobState.BLOCKED)) == 2
    assert await ctx.store.list(state=JobState.PENDING) == ()
    for job in await ctx.store.list():
        await ctx.store.resume_blocked(job.event_id, ctx.clock.now())
    worker = OutboxWorker(ctx.store, ctx.delivery, ctx.clock)
    for _ in range(2):
        acknowledged = await worker.run_once()
        assert acknowledged.state == JobState.ACKNOWLEDGED
        assert acknowledged.receipt.mode == "demo_journal"
    assert len(await entries(ctx)) == 2


async def test_demo_journal_deduplicates_and_contains_no_patient_details(outbox_context):
    ctx = outbox_context
    jobs = await enqueue(ctx)
    await asyncio.gather(*(ctx.delivery.deliver(jobs[0]) for _ in range(6)))
    await ctx.delivery.deliver(jobs[1])
    records = await entries(ctx)
    assert len(records) == 2
    sms = next(e for e in records if e["payload"]["kind"] == "cancellation_sms")
    assert "15.10 в 09:00 отменена" in sms["text"]
    assert not any(word in sms["text"] for word in ("терапевт", "Тестов", "01.01.1990"))
    released = next(e for e in records if e["payload"]["kind"] == "slot_released")
    assert "client_id" not in released["payload"] and released["text"] is None
    for job in jobs:
        assert await ctx.delivery.lookup(job) == journal_receipt(job.event_id)


async def test_known_temporary_failure_is_delayed_and_bounded(outbox_context):
    ctx = outbox_context
    jobs = await enqueue(ctx)
    calls = []

    class Temporary:
        async def deliver(self, job):
            calls.append(job.event_id)
            raise TemporaryDeliveryFailure(retry_after=10)

    worker = OutboxWorker(ctx.store, Temporary(), ctx.clock)
    for attempt in range(1, 6):
        results = [await worker.run_once(), await worker.run_once()]
        assert {j.attempts for j in results} == {attempt}
        assert {j.state for j in results} == {JobState.FAILED if attempt == 5 else JobState.RETRY}
        assert await worker.run_once() is None
        if attempt < 5:
            until = max(j.available_at for j in results)
            ctx.clock.value = until - timedelta(microseconds=1)
            assert await worker.run_once() is None
            ctx.clock.value = until
    assert len(calls) == 10 and set(calls) == {j.event_id for j in jobs}
    assert await entries(ctx) == ()


@pytest.mark.parametrize("failure", ["unknown", "unexpected", "permanent", "wrong_receipt"])
async def test_uncertain_or_permanent_result_is_not_resent(outbox_context, failure):
    ctx = outbox_context
    await enqueue(ctx)
    calls = []

    class Broken:
        async def deliver(self, job):
            calls.append(job.event_id)
            if failure == "unknown":
                raise DeliveryUnknown()
            if failure == "unexpected":
                raise RuntimeError("секретные сведения провайдера не должны попасть в очередь")
            if failure == "permanent":
                raise PermanentDeliveryFailure()
            return journal_receipt("0" * 32)

        async def lookup(self, job):
            return None

    worker = OutboxWorker(ctx.store, Broken(), ctx.clock)
    for _ in range(2):
        job = await worker.run_once()
        assert job.state == (JobState.FAILED if failure == "permanent" else JobState.UNKNOWN)
        assert "секрет" not in job.model_dump_json()
        if failure != "permanent":
            assert (await worker.reconcile(job.event_id)).state == JobState.UNKNOWN
    ctx.clock.value += timedelta(days=1)
    assert await worker.run_once() is None and len(calls) == 2


async def test_lost_receipt_is_reconciled_without_another_delivery(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    calls = []

    class Lost:
        async def deliver(self, job):
            calls.append(job.event_id)
            await ctx.delivery.deliver(job)
            raise DeliveryUnknown()

        async def lookup(self, job):
            return await ctx.delivery.lookup(job)

    worker = OutboxWorker(ctx.store, Lost(), ctx.clock)
    pending = [await worker.run_once(), await worker.run_once()]
    assert await worker.run_once() is None
    reopened = MemoryOutboxStore(ctx.ledger) if ctx.ledger else PostgresOutboxStore(ctx.engine)
    worker = OutboxWorker(reopened, Lost(), ctx.clock)
    for job in pending:
        assert (await worker.reconcile(job.event_id)).state == JobState.ACKNOWLEDGED
        assert (await worker.reconcile(job.event_id)).state == JobState.ACKNOWLEDGED
    assert len(calls) == 2 and len(await entries(ctx)) == 2


async def test_task_cancellation_leaves_lease_for_safe_recovery(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    started = asyncio.Event()

    class Paused:
        async def deliver(self, job):
            started.set()
            await asyncio.Event().wait()

    worker = OutboxWorker(ctx.store, Paused(), ctx.clock)
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ctx.clock.value += timedelta(seconds=30)
    other = await ctx.store.claim(ctx.clock.now())
    assert other is not None
    assert sum(j.state == JobState.UNKNOWN for j in await ctx.store.list()) == 1
    assert await entries(ctx) == ()


async def test_slow_worker_cannot_overwrite_expired_lease_but_receipt_can_be_reconciled(
    outbox_context,
):
    ctx = outbox_context
    await enqueue(ctx)
    started, finish = asyncio.Event(), asyncio.Event()
    sent = []

    class Slow:
        async def deliver(self, job):
            started.set()
            await finish.wait()
            sent.append(job.event_id)
            return await ctx.delivery.deliver(job)

        async def lookup(self, job):
            return await ctx.delivery.lookup(job)

    worker = OutboxWorker(ctx.store, Slow(), ctx.clock)
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    ctx.clock.value += timedelta(seconds=30)
    # Получатель не удерживает замок очереди; второй обработчик может взять другое задание.
    assert await asyncio.wait_for(ctx.store.claim(ctx.clock.now()), 2) is not None
    finish.set()
    with pytest.raises(OutboxError, match="LEASE_LOST"):
        await task
    assert len(sent) == 1
    assert (await worker.reconcile(sent[0])).state == JobState.ACKNOWLEDGED
    assert len(await entries(ctx)) == 1


async def test_journal_key_cannot_be_reused_with_a_different_payload(outbox_context):
    ctx = outbox_context
    jobs = await enqueue(ctx)
    job = next(j for j in jobs if j.payload.kind == EventKind.CANCELLATION_SMS)
    await ctx.delivery.deliver(job)
    forged = job.model_copy(update={"payload": job.payload.model_copy(update={"client_id": 2})})
    with pytest.raises(OutboxError, match="JOURNAL_CONFLICT"):
        await ctx.delivery.deliver(forged)
    assert len(await entries(ctx)) == 1


async def test_reconcile_expired_worker_does_not_claim_another_job(outbox_context):
    ctx = outbox_context
    await enqueue(ctx)
    job = await ctx.store.claim(ctx.clock.now(), 1)
    await ctx.delivery.deliver(job)
    ctx.clock.value += timedelta(seconds=1)
    worker = OutboxWorker(ctx.store, ctx.delivery, ctx.clock)
    assert (await worker.reconcile(job.event_id)).state == JobState.ACKNOWLEDGED
    assert len(await ctx.store.list(state=JobState.PENDING)) == 1
    assert await ctx.store.recover_expired(ctx.clock.now()) == 0
    assert len(await entries(ctx)) == 1


def test_comment_mark_freezes_gmt4_time_and_uses_exact_visit_id():
    clock = DemoClock()
    job = appointment_sms_event("action-1", 1, 17, clock.now(), clock.now())
    prepared = prepare_comment_event(job, clock.now())
    assert prepared.payload.expected_line == "SMS Запись создана #17 14.10.2026 09:00"
    changed = prepared.model_copy(
        update={"state": JobState.RETRY, "available_at": clock.now() + timedelta(minutes=1)}
    )
    assert (
        prepare_comment_event(changed, clock.now() + timedelta(minutes=1)).payload.expected_line
        == prepared.payload.expected_line
    )

    waitlist = waitlist_sms_event("wait-1", 1, clock.now())
    assert (
        prepare_comment_event(waitlist, clock.now()).payload.expected_line
        == "SMS Лист ожидания 14.10.2026 09:00"
    )


class _CommentMis:
    def __init__(self, comment, *, apply_then_timeout=False):
        self.comment = comment
        self.patches = 0
        self.apply_then_timeout = apply_then_timeout

    @asynccontextmanager
    async def prepaid(self, *_args, **_kwargs):
        yield

    async def client(self, client_id, *, fresh=False):
        assert client_id == 1 and fresh
        return Client(
            id=client_id,
            last_name="Тестов",
            first_name="Тест",
            middle_name=None,
            birth_date="01.01.1990",
            comment=self.comment,
            created_at="15.10.2026 09:00",
        )

    async def update_client_comment(self, client_id, request):
        assert client_id == 1
        self.patches += 1
        self.comment = request.comment
        result = await self.client(client_id, fresh=True)
        if self.apply_then_timeout:
            from app.mis.ports import MisOutcomeUnknown

            raise MisOutcomeUnknown("TRANSPORT_FAILURE")
        return result


class _CommentLock:
    def __init__(self, opted_out=False):
        self.opted_out = opted_out

    async def lock_comment_client(self, *_args, **_kwargs):
        return True

    async def unlock_client(self, *_args):
        return None

    async def notification_opted_out(self, _client_id):
        return self.opted_out


async def test_comment_delivery_preserves_existing_comment_and_reconciles_unknown_read_only():
    clock = DemoClock()
    job = prepare_comment_event(
        appointment_sms_event("action-2", 1, 17, clock.now(), clock.now()), clock.now()
    )
    mis = _CommentMis("Исходный комментарий\nважная запись", apply_then_timeout=True)
    delivery = PostgresClientCommentDelivery(mis, _CommentLock(), clock)
    with pytest.raises(DeliveryUnknown):
        await delivery.deliver(job)
    assert mis.patches == 1
    assert (
        mis.comment
        == "Исходный комментарий\nважная запись\nSMS Запись создана #17 14.10.2026 09:00"
    )
    receipt = await delivery.lookup(job)
    assert receipt.event_id == job.event_id and receipt.mode == "client_comment"
    assert mis.patches == 1


@pytest.mark.parametrize("final_length", [1000, 1001])
async def test_comment_delivery_respects_exact_comment_limit(final_length):
    clock = DemoClock()
    job = prepare_comment_event(
        appointment_sms_event("action-3", 1, 17, clock.now(), clock.now()), clock.now()
    )
    old_comment = "x" * (final_length - len(job.payload.expected_line) - 1)
    mis = _CommentMis(old_comment)
    delivery = PostgresClientCommentDelivery(mis, _CommentLock(), clock)
    if final_length == 1000:
        await delivery.deliver(job)
        assert len(mis.comment) == 1000
        assert mis.comment.startswith(old_comment + "\n")
        assert mis.patches == 1
    else:
        with pytest.raises(PermanentDeliveryFailure) as failure:
            await delivery.deliver(job)
        assert failure.value.code == "COMMENT_TOO_LONG"
        assert mis.patches == 0 and mis.comment == old_comment


async def test_comment_delivery_respects_notification_opt_out():
    clock = DemoClock()
    job = prepare_comment_event(
        appointment_sms_event("action-4", 1, 17, clock.now(), clock.now()), clock.now()
    )
    mis = _CommentMis("старый комментарий")
    delivery = PostgresClientCommentDelivery(mis, _CommentLock(opted_out=True), clock)
    with pytest.raises(PermanentDeliveryFailure) as failure:
        await delivery.deliver(job)
    assert failure.value.code == "NOTIFICATION_OPTED_OUT"
    assert mis.patches == 0 and mis.comment == "старый комментарий"


async def test_comment_delivery_blocks_unassigned_released_slot(outbox_context):
    ctx = outbox_context
    released = cancellation_events(ctx.action, ctx.clock.now())[1]
    ledger = MemoryLedger()
    ledger.outbox[released.event_id] = released
    store = MemoryOutboxStore(ledger)
    mis = _CommentMis("старый комментарий")
    delivery = PostgresClientCommentDelivery(mis, _CommentLock(), ctx.clock)
    job = await OutboxWorker(store, delivery, ctx.clock).run_once()
    assert job.state == JobState.BLOCKED
    assert job.last_error == "SLOT_OFFER_NOT_CONFIGURED"
    assert mis.patches == 0 and mis.comment == "старый комментарий"
