"""Приёмка T08: обновление старой БД и независимые исполнители без настоящей МИС."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select

from app.dialogue.models import DialogueState
from app.handoffs.models import MisDeliveryState
from app.handoffs.service import HandoffService, HandoffSettings
from app.main import create_app
from app.mis.models import Escalation
from app.settings import Settings
from app.storage.booking import migrate
from app.storage.handoffs import PostgresHandoffStore
from app.storage.schema import actions, metadata, schema_versions
from tests.test_handoffs_r01 import Clock


@pytest.mark.asyncio
async def test_migrate_schema_nine_to_ten_preserves_existing_booking(pg_blank_engine):
    # Версия 9 отличается отсутствием шести добавочных таблиц T08.
    old_tables = [
        table
        for table in metadata.sorted_tables
        if not table.name.startswith(("backend_handoff_", "backend_callback_"))
    ]
    clock = Clock()
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=old_tables))
        await connection.execute(insert(schema_versions).values(version=9))
        await connection.execute(
            insert(actions).values(
                action_id="a" * 32,
                call_id="old-call",
                revision=1,
                client_id=71,
                doctor_id=6,
                scheduled_at=clock.now() + timedelta(days=2),
                specialty="лор",
                expires_at=clock.now() + timedelta(minutes=2),
                state="booked",
                confirmed_turn=5,
                visit_id=257,
            )
        )
        before = (await connection.execute(select(actions))).mappings().all()
    # Повтор миграции также должен быть безопасен.
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [10]
        assert (await connection.execute(select(actions))).mappings().all() == before
    assert await PostgresHandoffStore(pg_blank_engine).list_requests(limit=50, offset=0) == []


class MisSpy:
    def __init__(self):
        self.posts = 0
        self.reads = 0

    async def create_escalation(self, payload):
        self.posts += 1
        await asyncio.sleep(0.02)
        return Escalation(
            id=42,
            client_id=payload.client_id,
            description=payload.description,
            created_at="19.10.2026 10:00",
        )

    async def escalations(self, client_id, *, limit, offset):
        self.reads += 1
        return []


async def connected_request(engine, mis):
    service = HandoffService(
        PostgresHandoffStore(engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    event = SimpleNamespace(
        attempt_id="verified-attempt", call_id="verified-call", route="R-08", priority="P3"
    )
    await service.transfer_started(event, DialogueState(call_id=event.call_id, client_id=71))
    await service.transfer_result(SimpleNamespace(**vars(event), outcome="connected", reason=None))
    request = await service.store.request_for_call(event.call_id)
    assert request.mis_operation_id is not None
    return service, request


@pytest.mark.asyncio
async def test_two_postgres_store_instances_send_only_one_escalation(pg_engine):
    mis = MisSpy()
    first, request = await connected_request(pg_engine, mis)
    second = HandoffService(
        PostgresHandoffStore(pg_engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    await asyncio.gather(first.deliver_mis_once(), second.deliver_mis_once())
    assert mis.posts == 1
    result = await first.store.get_mis_operation(request.mis_operation_id)
    assert result.state == MisDeliveryState.APPLIED


@pytest.mark.asyncio
async def test_restarted_worker_reconciles_sending_without_post(pg_engine):
    mis = MisSpy()
    first, request = await connected_request(pg_engine, mis)
    operation = await first.store.get_mis_operation(request.mis_operation_id)
    await first.store.update_mis_operation(
        operation.model_copy(
            update={
                "state": MisDeliveryState.SENDING,
                "sending_at": Clock().now(),
                "next_check_at": Clock().now(),
            }
        ),
        expected_version=operation.version,
    )
    restarted = HandoffService(
        PostgresHandoffStore(pg_engine), mis, Clock(), b"x" * 32, HandoffSettings(enabled=True)
    )
    await restarted.deliver_mis_once()
    assert mis.posts == 0 and mis.reads == 1
    checked = await restarted.store.get_mis_operation(operation.operation_id)
    assert checked.state == MisDeliveryState.UNKNOWN


@pytest.mark.asyncio
async def test_enabled_operator_routes_require_team_login(pg_engine, tmp_path):
    """Настоящая фабрика HTTP-приложения, изолированный журнал, подмена только МИС."""
    service, _ = await connected_request(pg_engine, MisSpy())
    password = "fictional-operator-review-password"
    settings = Settings(
        _env_file=None,
        app_mode="hackathon",
        mis_token="fictional-token",
        dialogue_enabled=True,
        dialogue_mode="natural",
        dialogue_storage="postgres",
        schedule_index_path=tmp_path / "fixture.sqlite",
        dialogue_hash_key="x" * 32,
        web_access_enabled=True,
        web_access_password=password,
        handoffs_enabled=True,
        callback_enabled=True,
    )

    class Database:
        async def ping(self):
            pass

        async def aclose(self):
            pass

    @asynccontextmanager
    async def dialogue_factory(settings, clock):
        yield SimpleNamespace(handoffs=service)

    app = create_app(
        settings, database_factory=lambda _: Database(), dialogue_factory=dialogue_factory
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for path in ("/operator", "/api/operator/requests", "/api/operator/callbacks"):
                denied = await client.get(path)
                assert denied.status_code == 401
                allowed = await client.get(path, auth=("clinic", password))
                assert allowed.status_code == 200
            response = await client.get("/api/operator/requests", auth=("clinic", password))
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["items"][0]["transfer_state"] == "connected"
