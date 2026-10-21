from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from app.booking.preferences import SlotPreferences
from app.storage.booking import migrate
from app.storage.schema import metadata, schema_versions, waitlist_requests
from app.storage.waitlist import PostgresWaitlistStore
from app.waitlist.memory import MemoryWaitlistStore
from app.waitlist.models import WaitlistRequest

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=4)))


def request(*, created_at=NOW, time_from=time(7, 30)):
    return WaitlistRequest(
        request_id="a" * 64,
        call_id="waitlist-call",
        turn_id=4,
        client_id=71,
        specialty="невролог",
        preferences=SlotPreferences(
            time_from=time_from,
            time_to=time(12),
            exclude_weekdays=(1,),
            date_from=date(2026, 10, 22),
        ),
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_memory_waitlist_is_idempotent_and_detects_request_reuse():
    store = MemoryWaitlistStore()
    first = await store.create(request())
    replay = await store.create(request(created_at=NOW + timedelta(minutes=1)))
    assert replay == first
    assert await store.by_call("waitlist-call") == (first,)
    with pytest.raises(ValueError, match="WAITLIST_REQUEST_CONFLICT"):
        await store.create(request(time_from=time(9)))


@pytest.mark.asyncio
async def test_migration_ten_to_eleven_adds_waitlist_without_data_loss(pg_blank_engine):
    old_tables = [table for table in metadata.sorted_tables if table is not waitlist_requests]
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=old_tables))
        await connection.execute(insert(schema_versions).values(version=10))

    await migrate(pg_blank_engine)
    async with pg_blank_engine.connect() as connection:
        assert await connection.scalar(select(schema_versions.c.version)) == 11


@pytest.mark.asyncio
async def test_postgres_waitlist_creation_is_idempotent(pg_engine):
    store = PostgresWaitlistStore(pg_engine)
    first = await store.create(request())
    replay = await store.create(request(created_at=NOW + timedelta(minutes=1)))
    assert replay == first
    assert await store.by_call("waitlist-call") == (first,)
