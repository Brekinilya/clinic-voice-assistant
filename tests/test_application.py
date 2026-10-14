"""Отказы зависимостей и жизненный цикл проверяем без настоящего стенда и его квоты."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.clock import CLINIC_TIMEZONE
from app.main import create_app
from app.ports import DatabaseUnavailable
from app.settings import Settings


class FixedClock:
    def now(self):
        return datetime(2026, 10, 14, 10, 0, tzinfo=CLINIC_TIMEZONE)


class TestDatabase:
    __test__ = False

    def __init__(self, error=None, delay=0):
        self.error = error
        self.delay = delay
        self.calls = 0
        self.closed = False
        self.cancelled = False

    async def ping(self):
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


def application(database, **settings):
    return create_app(
        Settings(_env_file=None, **settings),
        database_factory=lambda _: database,
        clock=FixedClock(),
    )


@asynccontextmanager
async def client_for(app):
    # HTTPX не запускает lifespan сам: явно воспроизводим запуск и остановку ASGI-сервиса.
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_liveness_survives_database_failure_without_querying_it():
    database = TestDatabase(error=DatabaseUnavailable("private-connection-string"))
    async with client_for(application(database)) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["mode"] == "offline_test"
    assert response.json()["checked_at"] == "2026-10-14T10:00:00+04:00"
    assert database.calls == 0
    assert database.closed


async def test_readiness_checks_database_and_closes_it():
    database = TestDatabase()
    async with client_for(application(database)) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ready"
    assert database.calls == 1
    assert database.closed


async def test_readiness_failure_does_not_expose_driver_error():
    database = TestDatabase(error=DatabaseUnavailable("secret-password"))
    async with client_for(application(database)) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert "secret-password" not in response.text


async def test_slow_probe_is_cancelled_and_reported_as_unavailable():
    database = TestDatabase(delay=10)
    async with client_for(application(database, database_probe_timeout_seconds=0.01)) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert database.cancelled
    assert database.closed


async def test_applications_do_not_share_resources():
    first = TestDatabase()
    second = TestDatabase(error=DatabaseUnavailable())
    async with client_for(application(first)) as one, client_for(application(second)) as two:
        assert (await one.get("/health/ready")).status_code == 200
        assert (await two.get("/health/ready")).status_code == 503
    assert first.closed and second.closed


async def test_database_created_during_startup_and_closed_after_request_exception():
    database = TestDatabase(error=RuntimeError("programming-error"))
    creations = []

    def factory(settings):
        creations.append(settings)
        return database

    app = create_app(Settings(_env_file=None), database_factory=factory)
    assert not creations
    async with client_for(app) as client:
        assert len(creations) == 1
        with pytest.raises(RuntimeError):
            await client.get("/health/ready")
    assert database.closed
    assert not hasattr(app.state, "runtime")
