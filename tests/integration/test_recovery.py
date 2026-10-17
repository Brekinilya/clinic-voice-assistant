"""T06: сохранённый результат, потеря процесса, два владельца и запрет повторного HTTP."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fixture_bridge import pipeline, turn
from test_budget_dialogue import reserve
from test_cancellation import SETUP as CANCEL_SETUP
from test_cancellation import prepare as prepare_cancel
from test_rescheduling import SETUP as MOVE_SETUP
from test_rescheduling import outbox
from test_rescheduling import prepare as prepare_move

from app.booking.models import ActionState
from app.mis.budget import Priority, current_demand
from app.mis.models import Visit, VisitCreate, VisitStatus
from app.mis.ports import BudgetExhausted
from app.recovery.coordinator import OperationCoordinator
from app.recovery.memory import MemoryRecoveryStore
from app.recovery.models import Outcome, OwnershipLost
from app.recovery.ownership import Scope, current
from app.recovery.worker import RecoveryWorker
from app.storage.recovery import PostgresRecoveryStore


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def recovery_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@asynccontextmanager
async def environment(engine, setup=None):
    async with pipeline(setup, pg_engine=engine) as (client, service, emulator, clock, *_):
        booking = service.booking
        cancel = service.conversation.cancellation.cancellation
        move = service.conversation.rescheduling.service
        store = (
            MemoryRecoveryStore(booking.store.ledger)
            if engine is None
            else PostgresRecoveryStore(engine)
        )
        coordinator = OperationCoordinator(store, clock)
        booking.operations = cancel.operations = move.operations = coordinator
        worker = RecoveryWorker(store, booking.mis, booking.store, cancel.store, move.store, clock)
        yield SimpleNamespace(
            client=client,
            service=service,
            emulator=emulator,
            clock=clock,
            store=store,
            coordinator=coordinator,
            worker=worker,
            booking=booking,
            cancel=cancel,
            move=move,
        )


def advance(env, seconds=121):
    env.clock.value += timedelta(seconds=seconds)


async def crash_before_post(env, monkeypatch):
    reserved = await reserve(env.client)

    async def crash(request):
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(env.booking.mis, "create_visit", crash)
        with pytest.raises(asyncio.CancelledError):
            await turn(env.client, 4, "Да")
    action = await env.booking.store.get(reserved["proposal"]["action_id"])
    assert action.state == ActionState.COMMITTING and env.emulator.write_count == 0
    return action


async def test_lost_post_is_found_after_new_worker_without_second_post(recovery_engine):
    async with environment(recovery_engine) as env:
        held = await reserve(env.client)
        env.emulator.next_failure = "lost_response"
        assert (await turn(env.client, 4, "Да"))["code"] == "RECONCILING"
        assert env.emulator.write_count == 1
        advance(env)
        if recovery_engine is not None:
            # Новый экземпляр не знает памяти прежнего процесса.
            env.worker.store = PostgresRecoveryStore(recovery_engine)
        result = await env.worker.run_once()
        action = await env.booking.store.get(held["proposal"]["action_id"])
        assert result.state == "watching" and action.state == ActionState.BOOKED
        assert action.visit_id == 1 and env.emulator.write_count == 1


async def test_crash_after_post_before_local_finish_is_recovered(recovery_engine, monkeypatch):
    async with environment(recovery_engine) as env:
        held = await reserve(env.client)
        original = env.booking.store.finish

        async def fail_finish(*args, **kwargs):
            raise RuntimeError("искусственное падение после успешного HTTP")

        with monkeypatch.context() as patch:
            patch.setattr(env.booking.store, "finish", fail_finish)
            assert (await turn(env.client, 4, "Да"))["code"] == "PROCESSING_INTERRUPTED"
        assert env.emulator.write_count == 1
        advance(env)
        assert (await env.worker.run_once()).state == "watching"
        assert (
            await env.booking.store.get(held["proposal"]["action_id"])
        ).state == ActionState.BOOKED
        assert env.booking.store.finish == original and env.emulator.write_count == 1


@pytest.mark.parametrize("matches", [0, 2])
async def test_zero_or_two_results_never_authorize_another_post(
    recovery_engine, monkeypatch, matches
):
    async with environment(recovery_engine) as env:
        action = await crash_before_post(env, monkeypatch)
        for identifier in range(10, 10 + matches):
            env.emulator.records["demo"][identifier] = Visit(
                id=identifier,
                client_id=action.client_id,
                doctor=env.emulator.doctors[action.slot.doctor_id],
                scheduled_at=action.slot.scheduled_at,
                status=VisitStatus.PLANNED,
                created_at=env.clock.now(),
                updated_at=env.clock.now(),
            )
        advance(env)
        result = await env.worker.run_once()
        if matches == 0:
            assert result.state == "pending"
            for _ in range(2):
                advance(env, 31)
                result = await env.worker.run_once()
        assert result.state == "manual"
        assert result.reason == ("AMBIGUOUS_VISITS" if matches else "RECONCILIATION_EXHAUSTED")
        assert (await env.booking.store.get(action.action_id)).state == ActionState.COMMITTING
        assert env.emulator.write_count == 0
        advance(env, 1000)
        assert await env.worker.run_once() is None  # Ручная очередь не начинает цикл заново.


async def test_two_recovery_workers_observe_one_operation_once(recovery_engine):
    async with environment(recovery_engine) as env:
        await reserve(env.client)
        env.emulator.next_failure = "lost_response"
        await turn(env.client, 4, "Да")
        advance(env)
        other = RecoveryWorker(
            env.store,
            env.booking.mis,
            env.booking.store,
            env.cancel.store,
            env.move.store,
            env.clock,
        )
        mark = len(env.emulator.log)
        results = await asyncio.gather(env.worker.run_once(), other.run_once())
        assert sum(r is not None for r in results) == 1
        assert len([r for r in env.emulator.log[mark:] if r[1:3] == ("GET", "visits")]) == 1
        assert env.emulator.write_count == 1


async def test_stale_owner_cannot_finish_domain_or_recovery_journal(recovery_engine, monkeypatch):
    async with environment(recovery_engine) as env:
        action = await crash_before_post(env, monkeypatch)
        old = await env.store.acquire("booking", action.action_id, env.clock.now(), seconds=10)
        advance(env, 11)
        new = await env.store.claim_next(env.clock.now())
        assert new.fence > old.fence
        token = current.set(Scope(env.store, old, env.clock))
        try:
            with pytest.raises(OwnershipLost):
                await env.booking.store.finish(action.action_id, ActionState.BOOKED, 10)
        finally:
            current.reset(token)
        with pytest.raises(OwnershipLost):
            await env.store.finish(old, env.clock.now(), Outcome(state="resolved"))
        assert (await env.booking.store.get(action.action_id)).state == ActionState.COMMITTING
        await env.store.finish(
            new, env.clock.now(), Outcome(state="manual", reason="TEST_NEW_OWNER")
        )


async def test_lease_is_checked_after_claim_before_http(recovery_engine, monkeypatch):
    async with environment(recovery_engine) as env:
        await reserve(env.client)
        send = env.booking.mis.create_visit
        leases = []

        async def expired_before_send(request):
            advance(env, env.coordinator.lease_s + 1)
            leases.append(await env.store.claim_next(env.clock.now()))
            return await send(request)

        monkeypatch.setattr(env.booking.mis, "create_visit", expired_before_send)
        result = await turn(env.client, 4, "Да")
        assert result["code"] != "BOOKED" and leases[0] is not None
        assert env.emulator.write_count == 0


async def test_background_lease_cannot_send_mutations(recovery_engine, monkeypatch):
    async with environment(recovery_engine) as env:
        action = await crash_before_post(env, monkeypatch)
        advance(env)
        lease = await env.store.claim_next(env.clock.now())
        token = current.set(Scope(env.store, lease, env.clock))
        try:
            with pytest.raises(OwnershipLost):
                await env.booking.mis.create_visit(
                    VisitCreate(
                        client_id=action.client_id,
                        doctor_id=action.slot.doctor_id,
                        scheduled_at=action.slot.scheduled_at,
                    )
                )
        finally:
            current.reset(token)
        assert env.emulator.write_count == 0


async def test_lost_cancel_patch_is_read_back_and_events_are_not_duplicated(recovery_engine):
    async with environment(recovery_engine, CANCEL_SETUP) as env:
        prepared = await prepare_cancel(env.client)
        env.emulator.next_failure = "lost_response"
        assert (await turn(env.client, 4, "Да"))["code"] == "CANCEL_RECONCILING"
        advance(env)
        assert (await env.worker.run_once()).state == "resolved"
        assert (
            await env.cancel.store.get(prepared["cancel_proposal"]["action_id"])
        ).state == "cancelled"
        jobs = await outbox(env.service, recovery_engine).list()
        assert len(jobs) == 2 and env.emulator.patch_count == 1
        advance(env)
        assert await env.worker.run_once() is None
        assert len(await outbox(env.service, recovery_engine).list()) == 2


async def test_lost_move_post_requires_fresh_yes_before_old_cancel(recovery_engine):
    async with environment(recovery_engine, MOVE_SETUP) as env:
        prepared = await prepare_move(env.client)
        env.emulator.next_failure = "lost_response"
        assert (await turn(env.client, 5, "Да"))["code"] == "MOVE_PENDING"
        advance(env)
        result = await env.worker.run_once()
        assert (result.state, result.reason) == ("manual", "FRESH_CONFIRMATION_REQUIRED")
        assert (
            await env.move.store.get(prepared["move_proposal"]["action_id"])
        ).state == "new_created"
        assert (
            env.emulator.patch_count == 0
            and env.emulator.records["demo"][1].status == VisitStatus.PLANNED
        )
        renewed = await turn(env.client, 6, "Проверьте перенос")
        assert renewed["move_proposal"]["stage"] == "finish" and env.emulator.patch_count == 0
        assert (await turn(env.client, 7, "Да"))["code"] == "RESCHEDULED"
        assert env.emulator.write_count == env.emulator.patch_count == 1


async def test_lost_move_cancel_is_completed_by_reads_only(recovery_engine):
    async with environment(recovery_engine, MOVE_SETUP) as env:
        await prepare_move(env.client)

        async def lose_patch(request):
            if request.method == "PATCH":
                env.emulator.next_failure = "lost_response"

        env.booking.mis._client.event_hooks["request"].append(lose_patch)
        assert (await turn(env.client, 5, "Да"))["code"] == "MOVE_PENDING"
        advance(env)
        assert (await env.worker.run_once()).state == "resolved"
        assert env.emulator.write_count == env.emulator.patch_count == 1
        assert len(await outbox(env.service, recovery_engine).list()) == 2


async def test_external_cancellation_archives_projection_without_our_notifications(recovery_engine):
    async with environment(recovery_engine) as env:
        held = await reserve(env.client)
        assert (await turn(env.client, 4, "Да"))["code"] == "BOOKED"
        visit = env.emulator.records["demo"][1]
        env.emulator.records["demo"][1] = visit.model_copy(update={"status": VisitStatus.CANCELLED})
        advance(env, 301)
        assert (await env.worker.run_once()).state == "resolved"
        assert (
            await env.booking.store.get(held["proposal"]["action_id"])
        ).state == ActionState.ARCHIVED
        assert not await outbox(env.service, recovery_engine).list()
        assert env.emulator.patch_count == 0


@pytest.mark.parametrize("retry_after", [90, 3600])
async def test_rate_limit_does_not_consume_observation_attempts(
    recovery_engine, monkeypatch, retry_after
):
    async with environment(recovery_engine) as env:
        await crash_before_post(env, monkeypatch)

        async def limited(**kwargs):
            raise BudgetExhausted("RATE_LIMIT", retry_after=retry_after)

        monkeypatch.setattr(env.booking.mis, "visits", limited)
        advance(env)
        result = await env.worker.run_once()
        assert result.state == "pending" and result.attempts == 0
        assert result.due_at == min(
            env.clock.now() + timedelta(seconds=retry_after),
            result.uncertain_since + timedelta(seconds=600),
        )
        assert await env.worker.run_once() is None


async def test_old_http_result_cannot_overwrite_recovered_success(recovery_engine):
    async with environment(recovery_engine) as env:
        held = await reserve(env.client)
        received, release = asyncio.Event(), asyncio.Event()

        async def pause_response(response):
            if response.request.method == "POST":
                received.set()
                await release.wait()

        env.booking.mis._client.event_hooks["response"].append(pause_response)
        original = asyncio.create_task(turn(env.client, 4, "Да"))
        try:
            await asyncio.wait_for(received.wait(), 3)
            assert await env.worker.run_once() is None  # Живого владельца не вытесняем.
            advance(env)
            assert (await env.worker.run_once()).state == "watching"
        finally:
            release.set()
        await asyncio.wait_for(original, 3)
        action = await env.booking.store.get(held["proposal"]["action_id"])
        assert action.state == ActionState.BOOKED and action.visit_id == 1
        assert env.emulator.write_count == 1
        assert (await env.store.list())[0].state == "watching"


async def test_projection_poll_leaves_budget_headroom_for_calls(recovery_engine):
    async with environment(recovery_engine) as env:
        await reserve(env.client)
        await turn(env.client, 4, "Да")
        priorities = []

        async def record_priority(request):
            priorities.append(current_demand().priority)

        env.booking.mis._client.event_hooks["request"].append(record_priority)
        advance(env, 301)
        assert (await env.worker.run_once()).state == "watching"
        assert priorities and set(priorities) == {Priority.BACKGROUND}


async def test_unavailable_mis_eventually_goes_to_manual_without_retrying_post(
    recovery_engine, monkeypatch
):
    async with environment(recovery_engine) as env:
        action = await crash_before_post(env, monkeypatch)
        before_expiry = action.expires_at

        async def limited(**kwargs):
            raise BudgetExhausted("RATE_LIMIT", retry_after=60)

        monkeypatch.setattr(env.booking.mis, "visits", limited)
        advance(env)
        assert (await env.worker.run_once()).state == "pending"
        advance(env, 601)
        result = await env.worker.run_once()
        assert (result.state, result.reason) == ("manual", "RECONCILIATION_EXHAUSTED")
        assert env.emulator.write_count == 0
        assert (await env.booking.store.get(action.action_id)).expires_at == before_expiry
