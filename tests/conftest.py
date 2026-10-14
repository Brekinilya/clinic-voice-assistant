"""Изолированные схемы настоящего PostgreSQL для всех интеграционных проверок."""

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.booking import migrate


@pytest_asyncio.fixture
async def pg_blank_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("настоящий PostgreSQL не задан: нужен TEST_DATABASE_URL")
    admin = create_async_engine(url, hide_parameters=True)
    schema = "test_backend_" + uuid4().hex
    engine = None
    try:
        async with admin.begin() as connection:
            # Идентификатор состоит только из нашего префикса и сгенерированного hex.
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url,
            pool_size=3,
            max_overflow=0,
            hide_parameters=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def pg_engine(pg_blank_engine):
    await migrate(pg_blank_engine)
    return pg_blank_engine
