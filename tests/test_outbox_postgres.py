"""Транзакция отмены, миграция схемы 3 и CLI на настоящей базе."""

from argparse import Namespace
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from test_outbox import enqueue
from test_outbox import outbox_context as outbox_context
from test_outbox import outbox_engine as outbox_engine

from app.cancellation.models import CancelState
from app.demo import DemoClock, demo_emulator
from app.mis.models import Visit, VisitStatus
from app.outbox import cli
from app.outbox.models import JobState
from app.settings import Settings
from app.storage.booking import migrate
from app.storage.cancellation import PostgresCancellationStore
from app.storage.outbox import PostgresOutboxStore
from app.storage.schema import (
    SCHEMA_VERSION,
    cancellations,
    delivery_journal,
    metadata,
    moves,
    outbox_jobs,
    schema_versions,
)


@pytest.mark.postgres
async def test_migration_three_preserves_cancellation_without_retroactive_jobs(
    pg_blank_engine,
):
    legacy = [t for t in metadata.sorted_tables if t not in (outbox_jobs, delivery_journal, moves)]
    clock = DemoClock()
    visit = Visit(
        id=1,
        client_id=1,
        doctor=demo_emulator(clock).doctors[1],
        scheduled_at=clock.now(),
        status=VisitStatus.PLANNED,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    action_id = uuid4().hex
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=legacy))
        await connection.execute(insert(schema_versions).values(version=3))
        await connection.execute(
            insert(cancellations).values(
                action_id=action_id,
                call_id="legacy",
                revision=1,
                visit_id=1,
                client_id=1,
                snapshot=visit.model_dump(mode="json"),
                expires_at=clock.now(),
                state="cancelled",
                confirmed_turn=1,
                completed_at=clock.now(),
                late=1,
            )
        )
    before = await PostgresCancellationStore(pg_blank_engine).get(action_id)
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    assert await PostgresCancellationStore(pg_blank_engine).get(action_id) == before
    assert await PostgresOutboxStore(pg_blank_engine).list() == ()
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
        assert list((await connection.execute(select(delivery_journal))).all()) == []


@pytest.mark.postgres
@pytest.mark.parametrize("outbox_engine", ["postgres"], indirect=True)
async def test_sql_failure_rolls_back_both_events_and_cancel_state(outbox_context, monkeypatch):
    ctx = outbox_context
    from app.storage import cancellation as module

    original = module.enqueue_cancellation

    async def fail_after_insert(*args):
        await original(*args)
        raise RuntimeError("тестовая ошибка после записи двух событий")

    monkeypatch.setattr(module, "enqueue_cancellation", fail_after_insert)
    with pytest.raises(RuntimeError):
        await enqueue(ctx)
    assert await ctx.store.list() == ()
    assert (await ctx.cancellation.get(ctx.action.action_id)).state == CancelState.COMMITTING
    monkeypatch.setattr(module, "enqueue_cancellation", original)
    assert len(await enqueue(ctx)) == 2


@pytest.mark.postgres
@pytest.mark.parametrize("outbox_engine", ["postgres"], indirect=True)
async def test_cli_demo_processing_and_status_are_explicit_and_persisted(
    outbox_context, monkeypatch
):
    ctx = outbox_context
    await enqueue(ctx)
    monkeypatch.setattr(cli, "backend_engine", lambda settings: ctx.engine)
    monkeypatch.setattr(cli, "ClinicClock", lambda: ctx.clock)
    settings = Settings(_env_file=None)
    disabled = Namespace(command="run-once", demo_journal=False, limit=2)
    result = await cli.execute(disabled, settings)
    assert {r["state"] for r in result["results"]} == {JobState.BLOCKED}
    for job in await ctx.store.list():
        await cli.execute(
            Namespace(command="resume-blocked", demo_journal=False, event_id=job.event_id), settings
        )
    logged = await cli.execute(Namespace(command="run-once", demo_journal=True, limit=2), settings)
    assert {r["mode"] for r in logged["results"]} == {"demo_journal"}
    assert {r["state"] for r in logged["results"]} == {JobState.ACKNOWLEDGED}
    status = await cli.execute(Namespace(command="status", demo_journal=False, limit=20), settings)
    assert status["states"] == {JobState.ACKNOWLEDGED: 2}
    assert all(set(job) == {"event_id", "kind", "state", "attempts"} for job in status["jobs"])
