"""T11 на настоящем PostgreSQL: гонка исполнителей, перезапуск процесса и миграция."""

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.reminders.models import Contact, ReminderJob
from app.storage.booking import migrate
from app.storage.reminders import PostgresReminderStore
from app.storage.schema import SCHEMA_VERSION, schema_versions
from tests.reminders.test_reminders import at

LEASE = timedelta(seconds=60)


def job(visit_id=7):
    return ReminderJob(
        visit_id=visit_id,
        client_id=3,
        scheduled_at=at(20, 10),
        call_day=date(2026, 10, 19),
        next_at=at(19, 10),
    )


@pytest.mark.postgres
async def test_two_executors_racing_for_one_job_make_one_attempt(pg_engine):
    store = PostgresReminderStore(pg_engine)
    item = job()
    assert await store.add_job(item)
    results = await asyncio.gather(
        *(store.claim(item.id, f"vm-{n}", at(19, 10, 5), LEASE) for n in range(5))
    )
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1 and claimed[0].number == 1
    assert (await store.job(item.id)).attempts == 1


@pytest.mark.postgres
async def test_same_visit_and_time_is_one_job(pg_engine):
    store = PostgresReminderStore(pg_engine)
    assert await store.add_job(job())
    assert not await store.add_job(job())
    moved = job().model_copy(update={"id": "moved", "scheduled_at": at(21, 10)})
    assert await store.add_job(moved)


@pytest.mark.postgres
async def test_state_survives_a_new_connection_pool(pg_engine):
    """Перезапуск процесса: новое подключение видит попытку и её lease."""
    store = PostgresReminderStore(pg_engine)
    item = job()
    await store.add_job(item)
    attempt = await store.claim(item.id, "vm-1", at(19, 10, 5), LEASE)
    await store.advance(attempt.id, "vm-1", "dialing", at(19, 10, 5), LEASE)
    await store.mark_planned(date(2026, 10, 19))
    url = pg_engine.url
    search_path = await _search_path(pg_engine)
    fresh = create_async_engine(url, connect_args={"server_settings": {"search_path": search_path}})
    try:
        restarted = PostgresReminderStore(fresh)
        assert await restarted.planned(date(2026, 10, 19))
        [stale] = await restarted.stale(at(19, 10, 5) + LEASE)
        assert stale.id == attempt.id and stale.state == "dialing"
        await restarted.recover(attempt.id, at(19, 10, 5) + LEASE)
        assert (await restarted.job(item.id)).state == "manual"
        # Повторное восстановление и поздний исход ничего не меняют
        await restarted.recover(attempt.id, at(19, 11))
        assert not await restarted.finish(attempt.id, "CONFIRMED", None, at(19, 11), {})
    finally:
        await fresh.dispose()


async def _search_path(engine):
    async with engine.connect() as connection:
        return (await connection.execute(text("SHOW search_path"))).scalar_one()


@pytest.mark.postgres
async def test_new_contact_does_not_clear_opt_out(pg_engine):
    store = PostgresReminderStore(pg_engine)
    await store.save_contact(Contact(client_id=3, endpoint="PJSIP/phone-021@gateway"))
    await store.opt_out(3, at(19, 10))
    await store.save_contact(Contact(client_id=3, endpoint="PJSIP/phone-021@gateway"))
    assert (await store.contact(3)).opt_out_at == at(19, 10)


@pytest.mark.postgres
async def test_migration_on_an_existing_schema_adds_reminder_tables(pg_blank_engine):
    await migrate(pg_blank_engine)
    async with pg_blank_engine.begin() as connection:
        for table in (
            "reminder_confirmations",
            "reminder_contacts",
            "reminder_attempts",
            "reminder_jobs",
            "reminder_days",
        ):
            await connection.execute(text(f"DROP TABLE {table}"))
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    async with pg_blank_engine.connect() as connection:
        names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
    assert {"reminder_jobs", "reminder_attempts", "reminder_contacts"} <= set(names)
    assert versions == [SCHEMA_VERSION]


@pytest.mark.postgres
async def test_reminder_tables_hold_no_names_or_phone_numbers_in_jobs(pg_engine):
    async with pg_engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                table: {c["name"] for c in inspect(sync).get_columns(table)}
                for table in ("reminder_jobs", "reminder_attempts")
            }
        )
    forbidden = {"last_name", "first_name", "middle_name", "phone", "endpoint", "text"}
    assert not any(forbidden & names for names in columns.values())
