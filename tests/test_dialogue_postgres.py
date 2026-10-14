"""Одинаковый контракт памяти и PostgreSQL, плюс миграция и восстановление после рестарта."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import insert, select

from app.booking.models import ActionState
from app.demo import DemoClock, DemoSchedule
from app.dialogue import bootstrap as dialogue_bootstrap
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import DialogueError, Phase, reply
from app.main import create_app
from app.settings import Settings
from app.storage.booking import PostgresBookingStore, migrate
from app.storage.dialogue import PostgresDialogueStore
from app.storage.schema import (
    SCHEMA_VERSION,
    actions,
    budget_blocks,
    budget_requests,
    dialogue_receipts,
    metadata,
    schema_versions,
)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def dialogue_store(request):
    return (
        MemoryDialogueStore()
        if request.param == "memory"
        else PostgresDialogueStore(request.getfixturevalue("pg_engine"))
    )


async def test_receipt_has_one_owner_and_detects_concurrent_conflicts(dialogue_store):
    results = await asyncio.gather(
        *(dialogue_store.claim("call-1", 1, "digest-1") for _ in range(12))
    )
    assert sum(c.acquired for c in results) == 1
    assert all(c.state.version == 1 for c in results)
    assert (await dialogue_store.inspect("call-1")).pending_turn_id == 1
    with pytest.raises(DialogueError, match="TURN_CONTENT_CONFLICT"):
        await dialogue_store.claim("call-1", 1, "different")
    with pytest.raises(DialogueError, match="TURN_IN_PROGRESS"):
        await dialogue_store.claim("call-1", 2, "digest-2")


async def test_completed_receipt_replays_exactly_and_new_turn_is_ordered(dialogue_store):
    claim = await dialogue_store.claim("call-1", 1, "digest-1")
    state = claim.state.model_copy(update={"phase": Phase.IDENTIFIED, "client_id": 1})
    result = reply(state, 1, "Пациент найден.")
    assert await dialogue_store.complete(state, 1, result)
    replay = await dialogue_store.claim("call-1", 1, "digest-1")
    assert not replay.acquired and replay.cached == result
    assert replay.state.last_turn_id == 1 and replay.state.pending_turn_id is None
    with pytest.raises(DialogueError, match="TURN_OUT_OF_ORDER"):
        await dialogue_store.claim("call-1", 3, "digest-3")
    assert (await dialogue_store.claim("call-1", 2, "digest-2")).acquired


async def test_ending_invalidates_inflight_completion_and_is_idempotent(dialogue_store):
    claim = await dialogue_store.claim("call-1", 1, "digest-1")
    ended = await dialogue_store.end("call-1")
    assert ended.phase == Phase.ENDED and ended.pending_turn_id is None
    assert await dialogue_store.end("call-1") == ended
    assert not await dialogue_store.complete(
        claim.state, 1, reply(claim.state, 1, "Поздний результат.")
    )
    replay = await dialogue_store.claim("call-1", 1, "digest-1")
    assert replay.cached.phase == Phase.ENDED
    with pytest.raises(DialogueError, match="CALL_ENDED"):
        await dialogue_store.claim("call-1", 2, "digest-2")


@pytest.mark.postgres
async def test_postgres_restart_preserves_state_and_pending_receipts(pg_engine):
    first = PostgresDialogueStore(pg_engine)
    claim = await first.claim("call-1", 1, "hmac-digest")
    result = reply(claim.state, 1, "Готово к выбору.")
    await first.complete(claim.state, 1, result)
    restarted = PostgresDialogueStore(pg_engine)
    assert (await restarted.claim("call-1", 1, "hmac-digest")).cached == result
    assert (await restarted.inspect("call-1")).last_turn_id == 1
    await restarted.claim("call-1", 2, "second-digest")
    again = await PostgresDialogueStore(pg_engine).claim("call-1", 2, "second-digest")
    assert not again.acquired and again.cached is None
    assert (await restarted.inspect("call-1")).pending_turn_id == 2
    async with pg_engine.connect() as connection:
        records = list((await connection.execute(select(dialogue_receipts))).mappings())
        assert all(set(r) == {"call_id", "turn_id", "digest", "reply"} for r in records)


@pytest.mark.postgres
async def test_upgrade_v1_keeps_existing_holds_and_is_repeatable(pg_blank_engine):
    legacy = [schema_versions, actions, budget_blocks, budget_requests]
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda c: metadata.create_all(c, tables=legacy))
        await connection.execute(insert(schema_versions).values(version=1))
    clock = DemoClock()
    store = PostgresBookingStore(pg_blank_engine)
    action = await store.hold("legacy-call", 1, DemoSchedule(clock.now()).slots()[0], clock.now())
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    assert (await store.get(action.action_id)).state == ActionState.HELD
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
    assert (await PostgresDialogueStore(pg_blank_engine).claim("new-call", 1, "digest")).acquired


@pytest.mark.postgres
async def test_receipt_does_not_keep_connection_while_worker_runs(pg_engine):
    store = PostgresDialogueStore(pg_engine)
    claims = await asyncio.gather(*(store.claim(f"call-{i}", 1, "digest") for i in range(6)))
    assert all(c.acquired for c in claims)
    # Даже при шести незавершённых командах все три соединения пула свободны.
    assert pg_engine.pool.checkedout() == 0
    results = await asyncio.wait_for(
        asyncio.gather(*(store.complete(c.state, 1, reply(c.state, 1, "Готово.")) for c in claims)),
        timeout=5,
    )
    assert all(results)


@pytest.mark.postgres
async def test_default_postgres_http_factory_restores_call_after_restart(pg_engine, monkeypatch):
    monkeypatch.setattr(dialogue_bootstrap, "backend_engine", lambda _: pg_engine)
    settings = Settings(
        _env_file=None,
        backend_demo_enabled=True,
        dialogue_storage="postgres",
        dialogue_hash_key="fictional-stable-key-for-test-at-least-32",
    )
    clock = DemoClock()

    @asynccontextmanager
    async def start():
        app = create_app(settings, clock=clock)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                yield client

    async with start() as client:
        for number, command in enumerate(
            ("пациент Тестов; Александр; Примерович; 01.01.1990", "варианты терапевт", "выбрать 1"),
            1,
        ):
            response = await client.post(
                "/api/dialog/call-1/turn", json={"turn_id": number, "text": command}
            )
            assert response.status_code == 200
        original = response.json()
        assert original["phase"] == "held"
    async with start() as client:
        replay = await client.post(
            "/api/dialog/call-1/turn", json={"turn_id": 3, "text": "выбрать 1"}
        )
        assert replay.json() == original
        assert (await client.get("/api/dialog/call-1")).json()["proposal"]["action_id"] == original[
            "proposal"
        ]["action_id"]
        assert (await client.post("/api/dialog/call-1/end")).json()["phase"] == "ended"
    action = await PostgresBookingStore(pg_engine).get(original["proposal"]["action_id"])
    assert action.state == ActionState.CANCELLED
