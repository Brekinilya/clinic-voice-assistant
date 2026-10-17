"""Сборка ресурсов тоже проверяется: ошибки имён настроек не должны ждать живого стенда."""

from contextlib import asynccontextmanager

import httpx

from app import backend
from app.demo import DemoClock, DemoSchedule, demo_emulator
from app.settings import Settings
from app.storage.schema import SCHEMA_VERSION


async def test_live_backend_uses_configured_budget_and_closes_resources(monkeypatch):
    class Rows:
        def scalars(self):
            return [SCHEMA_VERSION]

    class Connection:
        async def execute(self, query):
            return Rows()

    class Engine:
        closed = False

        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            self.closed = True

    engine = Engine()
    clock = DemoClock()
    emulator = demo_emulator(clock)
    client = httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    )
    monkeypatch.setattr(backend, "backend_engine", lambda _: engine)
    monkeypatch.setattr(backend.HttpMedicalSystem, "live_client", lambda _: client)
    settings = Settings(
        _env_file=None,
        app_mode="hackathon",
        mis_token="fictional-team-token",
        mis_rate_limit_per_min=5,
    )
    async with backend.open_backend(settings, DemoSchedule(clock.now())) as service:
        assert service.mis._budget.limit == 5
        assert not engine.closed and not client.is_closed
    assert engine.closed and client.is_closed and emulator.write_count == 0
