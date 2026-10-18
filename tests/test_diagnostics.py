"""Диагностика реального пути: защита доступа, разбор, МИС, независимые процессы."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from app.diagnostics.recorder import EventStore, Recorder, observe
from app.main import create_app
from app.mis.http import HttpMedicalSystem
from app.mis.ports import BudgetExhausted
from app.settings import Settings
from app.storage.booking import migrate
from app.storage.schema import SCHEMA_VERSION, debug_events, schema_versions
from tests.integration.fixture_bridge import pipeline, turn

KEY = b"diagnostic-test-key-not-a-secret!!"


@asynccontextmanager
async def recording(engine=None, capacity=512):
    recorder = Recorder(EventStore(KEY, engine), capacity=capacity)
    task = asyncio.create_task(recorder.run())
    try:
        yield recorder
        await recorder.flush()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def settings(**changes):
    values = dict(
        _env_file=None,
        backend_demo_enabled=True,
        dialogue_mode="natural",
        debug_panel_enabled=True,
        web_access_enabled=True,
        web_access_password="fictional-password-at-least-24-bytes",
    )
    return Settings(**(values | changes))


def test_debug_requires_team_login():
    with pytest.raises(ValidationError, match="WEB_ACCESS_ENABLED"):
        settings(web_access_enabled=False)


async def test_debug_ui_and_api_require_login_and_are_disabled_by_default():
    for enabled in (False, True):
        app = create_app(settings(debug_panel_enabled=enabled))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                assert (await client.get("/debug")).status_code == 401
                assert (await client.get("/api/debug/calls")).status_code == 401
                client.auth = ("clinic", "fictional-password-at-least-24-bytes")
                response = await client.get("/debug")
                assert response.status_code == (200 if enabled else 404)
                if enabled:
                    assert response.headers["cache-control"] == "no-store"
                    assert "Распознавание" in response.text
                    assert (await client.get("/debug/panel.js")).status_code == 200
                    assert (await client.get("/debug/unknown")).status_code == 404
                    assert (
                        await client.get("/api/debug/calls/call-1/events?limit=10000")
                    ).status_code == 422
                    await client.post(
                        "/api/dialog/text-demo/turn",
                        json={"turn_id": 1, "text": "Как работает поликлиника?"},
                    )
                    await app.state.diagnostics.flush()
                    events = (await client.get("/api/debug/calls/text-demo/events")).json()[
                        "events"
                    ]
                    assert any(e["kind"] == "dialogue.reply" for e in events)


async def test_trace_follows_real_dialogue_and_mis_without_changing_result():
    async with pipeline({}) as (client, service, emulator, *_):
        async with recording() as recorder:
            service.debug = recorder
            result = await turn(client, 1, "Хочу записаться к терапевту")
            assert result["phase"] == "identifying"
            await turn(client, 2, "Смирнова Ольга Викторовна, 12.03.1978")
            await recorder.flush()
            events = await recorder.store.read("scenario")
            kinds = [e["kind"] for e in events]
            assert "nlu.parsed" in kinds and "mis.sent" in kinds
            assert "dialogue.state" in kinds and "safety.checked" in kinds
            assert not emulator.records["demo"]
            assert any(e["data"].get("text", "").startswith("Смирнова") for e in events)
            assert not any("Смирнова" in row["payload"] for row in recorder.store.rows)
            for event in events:
                if event["kind"].startswith("mis."):
                    raw = json.dumps(event["data"], ensure_ascii=False)
                    assert "Смирнова" not in raw and "Authorization" not in raw
                    assert "1978" not in raw and "Bearer" not in raw
            await turn(client, 3, "Не записывайте разговор")
            await recorder.flush()
            private = await recorder.store.read("scenario")
            assert any(e["kind"] == "privacy.opt_out" for e in private)
            assert "Смирнова" not in str(private)
            service.debug = None


async def test_no_http_sent_when_budget_refuses_and_transport_metadata_has_no_secret():
    class Budget:
        async def reserve(self):
            raise BudgetExhausted("RATE_LIMIT", retry_after=12)

    async with (
        recording() as recorder,
        httpx.AsyncClient(base_url="https://offline.invalid") as client,
    ):
        mis = HttpMedicalSystem(client, Budget())
        with observe(recorder, "call-1", 1), pytest.raises(BudgetExhausted):
            await mis._request("GET", "clients/123?token=secret", params={"last_name": "Секрет"})
        await recorder.flush()
        events = await recorder.store.read("call-1")
        assert not any(e["kind"] == "mis.sent" for e in events)
        assert events[0]["data"]["resource"] == "clients/{id}"
        assert events[-2]["data"]["retry_after"] == 12
        assert "Секрет" not in str(events) and "secret" not in str(events)


async def test_broken_debug_database_never_blocks_dialogue():
    class BrokenStore(EventStore):
        async def append(self, events):
            raise RuntimeError("sensitive database detail")

    async with pipeline({}) as (client, service, *_), recording() as recorder:
        recorder.store = BrokenStore(KEY)
        service.debug = recorder
        result = await turn(client, 1, "Хочу записаться к терапевту")
        await recorder.flush()
        assert result["phase"] == "identifying"
        assert recorder.dropped > 0 and not recorder.healthy
        service.debug = None


async def test_queue_is_bounded_and_opt_out_clears_queued_texts():
    recorder = Recorder(EventStore(KEY), capacity=2)
    recorder.emit("call-1", 1, "dialogue.input", text="Секретное имя")
    recorder.emit("call-1", 1, "nlu.parsed", first_name="Секретное имя")
    recorder.opt_out("call-1")
    assert recorder.dropped == 1
    task = asyncio.create_task(recorder.run())
    try:
        await recorder.flush()
        events = await recorder.store.read("call-1")
        assert "Секретное имя" not in str(events)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.postgres
async def test_separate_producers_share_encrypted_cursor_and_migrate_v7(pg_engine):
    await pg_engine.dispose()
    async with pg_engine.begin() as conn:
        await conn.execute(update(schema_versions).values(version=7))
        await conn.execute(text("DROP TABLE backend_debug_events"))
    await migrate(pg_engine)
    async with pg_engine.connect() as conn:
        assert await conn.scalar(select(schema_versions.c.version)) == SCHEMA_VERSION
    async with recording(pg_engine) as voice, recording(pg_engine) as backend:
        voice.emit(
            "phone-1", 1, "voice.transcript", text="Иванов Иван Иванович", final=True, source="SIP"
        )
        backend.emit("text-1", 1, "dialogue.input", text="Другой звонок")
        await asyncio.gather(voice.flush(), backend.flush())
        reader = EventStore(KEY, pg_engine)
        calls = {item["call_id"]: item for item in await reader.calls()}
        assert calls["phone-1"]["source"] == "SIP" and len(calls) == 2
        first = await reader.read("phone-1", limit=1)
        assert first[0]["data"]["text"] == "Иванов Иван Иванович"
        voice.emit("phone-1", 1, "dialogue.reply", phase="booked")
        await voice.flush()
        more = await reader.read("phone-1", after=first[0]["id"])
        assert len(more) == 1 and more[0]["data"]["phase"] == "booked"
        async with pg_engine.connect() as conn:
            payloads = list((await conn.execute(select(debug_events.c.payload))).scalars())
            assert all("Иванов" not in p and "Другой звонок" not in p for p in payloads)
        async with pg_engine.begin() as conn:
            await conn.execute(
                update(debug_events).values(
                    created_at=datetime.now(timezone.utc) - timedelta(hours=2)
                )
            )
        assert await reader.read("phone-1") == []
        await reader.cleanup()
        async with pg_engine.connect() as conn:
            assert list((await conn.execute(select(debug_events))).all()) == []
