"""Общая короткая транзакция журналов создания и отмены, без HTTP внутри неё."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.recovery.ownership import ownership_fence


@asynccontextmanager
async def journal_transaction(engine: AsyncEngine):
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(142002)"))
        async with ownership_fence(connection):
            yield connection
