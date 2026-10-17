"""T05 с настоящим PostgreSQL: два звонка, фоновая задача и второй процесс в одном окне."""

import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval
from app.booking.service import BookingService
from app.clock import ClinicClock
from app.demo import DemoSchedule, demo_emulator
from app.mis.budget import Priority, demand
from app.mis.http import HttpMedicalSystem
from app.mis.models import parse_date
from app.mis.ports import BudgetExhausted
from app.storage.booking import PostgresBudget, migrate
from app.storage.schema import SCHEMA_VERSION, budget_requests, metadata, schema_versions

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fictional-team-token"
# Короткое окно вместо минуты: тот же алгоритм, тест идёт секунды.
WINDOW = 2.0
# Второй процесс приложения: свой пул соединений, своё событие и общий бюджет в PostgreSQL.
SECOND_PROCESS = """
import asyncio, json, os, sys, time
from datetime import timedelta
import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from app.clock import ClinicClock
from app.demo import demo_emulator
from app.mis.budget import demand
from app.mis.http import HttpMedicalSystem
from app.storage.booking import PostgresBudget

async def main():
    url, schema, start = os.environ["BUDGET_URL"], os.environ["BUDGET_SCHEMA"], float(os.environ["BUDGET_START"])
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    clock = ClinicClock()
    emulator = demo_emulator(clock)
    emulator.rate_limit, emulator.rate_window_s = 10, float(os.environ["BUDGET_WINDOW"])
    sent = []
    async def hook(request):
        sent.append(time.time())
    async with httpx.AsyncClient(base_url="https://offline.invalid/api/v1/", headers={"Authorization": "Bearer demo"}, transport=emulator.transport(), event_hooks={"request": [hook]}) as client:
        mis = HttpMedicalSystem(client, PostgresBudget(engine, "fictional-team-token", window_s=float(os.environ["BUDGET_WINDOW"])))
        await asyncio.sleep(max(0, start - time.time()))
        for _ in range(6):
            with demand(until=clock.now() + timedelta(seconds=10), owner="second-process"):
                await mis.doctors(fresh=True)
    await engine.dispose()
    print(json.dumps({"sent": sent, "limited": emulator.limited}))

asyncio.run(main())
"""


async def booking_call(service, call_id, client_id, birth_date, pick):
    def step():
        # Как в разговоре: ход ждёт окна не дольше 8 секунд.
        return demand(
            Priority.DIALOGUE, until=service.clock.now() + timedelta(seconds=8), owner=call_id
        )

    with step():
        patient = await service.identify(
            "Тестов", "Александр" if client_id == 1 else "Иван", "Примерович", birth_date
        )
    with step():
        choices = await service.options(patient.id, "терапевт", call_id=call_id)
    with step():
        action = await service.propose(call_id, patient.id, pick(choices))
    with step():
        return await service.confirm(
            Approval(
                call_id=call_id,
                action_id=action.action_id,
                revision=action.revision,
                turn_id=1,
                is_final=True,
                permitted=True,
            )
        )


async def background_job(mis, clock):
    with demand(Priority.BACKGROUND, until=clock.now() + timedelta(seconds=20), owner="catalog"):
        for _ in range(3):
            await mis.doctors(fresh=True)


@pytest.mark.postgres
async def test_two_calls_background_job_and_second_process_share_one_window(pg_engine):
    async with pg_engine.connect() as connection:
        schema = (await connection.execute(text("SHOW search_path"))).scalar_one()
    url = os.environ["TEST_DATABASE_URL"]
    # Отдельный пул соединений — как у второго экземпляра приложения.
    other_engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    clock = ClinicClock()
    emulator = demo_emulator(clock)
    emulator.rate_limit, emulator.rate_window_s = 10, WINDOW
    sent = []

    async def hook(request):
        sent.append(time.time())

    start = time.time() + 2.5
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        SECOND_PROCESS,
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "BUDGET_URL": url,
            "BUDGET_SCHEMA": schema,
            "BUDGET_START": str(start),
            "BUDGET_WINDOW": str(WINDOW),
        },
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=emulator.transport(),
            event_hooks={"request": [hook]},
        ) as client:
            services = [
                BookingService(
                    HttpMedicalSystem(
                        client, PostgresBudget(engine, TOKEN, window_s=WINDOW, clock=clock)
                    ),
                    MemoryBookingStore(),
                    DemoSchedule(clock.now()),
                    clock,
                )
                for engine in (pg_engine, other_engine)
            ]
            await asyncio.sleep(max(0, start - time.time()))
            began = time.time()
            first, second, _ = await asyncio.gather(
                booking_call(services[0], "call-1", 1, parse_date("01.01.1990"), lambda c: c[0]),
                booking_call(services[1], "call-2", 2, parse_date("02.01.1990"), lambda c: c[-1]),
                background_job(services[1].mis, clock),
            )
            finished = time.time()
        output, _ = await asyncio.wait_for(process.communicate(), 60)
    finally:
        if process.returncode is None:
            process.kill()
        await other_engine.dispose()
    assert process.returncode == 0
    other = json.loads(output)
    assert first.state == ActionState.BOOKED and second.state == ActionState.BOOKED
    assert first.slot != second.slot and emulator.write_count == 2
    # Ни одного 429: общий бюджет 8 держит всю команду под внешним лимитом 10.
    assert emulator.limited == 0 and other["limited"] == 0
    times = sorted(sent + other["sent"])
    busiest = max(sum(1 for u in times if t <= u < t + WINDOW) for t in times)
    assert len(times) > 16 and busiest <= 10
    # Запросов больше окна — значит, процессы действительно ждали друг друга.
    assert finished - began >= WINDOW


@pytest.mark.postgres
async def test_promise_survives_between_processes(pg_engine):
    async with pg_engine.connect() as connection:
        schema = (await connection.execute(text("SHOW search_path"))).scalar_one()
    other_engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        first = PostgresBudget(pg_engine, TOKEN, limit=2, window_s=WINDOW)
        second = PostgresBudget(other_engine, TOKEN, limit=2, window_s=WINDOW)
        await first.reserve(2)
        with demand(owner="call-a", until=ClinicClock().now()):
            with pytest.raises(BudgetExhausted):
                await first.reserve()
        await asyncio.sleep(WINDOW + 0.1)
        # Другой процесс видит обещанное место и не забирает его.
        with demand(owner="call-b"):
            with pytest.raises(BudgetExhausted):
                await second.reserve(2)
        with demand(owner="call-a"):
            assert await second.reserve()
    finally:
        await other_engine.dispose()


@pytest.mark.postgres
async def test_upgrade_from_version_five_adds_promises_and_keeps_the_window(pg_blank_engine):
    legacy = [t for t in metadata.sorted_tables if t is not budget_requests]
    async with pg_blank_engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(sync, tables=legacy))
        # Таблица окна версии 5 — без столбцов талонов.
        await connection.execute(
            text(
                "CREATE TABLE mis_budget_requests (id SERIAL PRIMARY KEY, scope VARCHAR(64) NOT NULL, "
                "requested_at TIMESTAMP WITH TIME ZONE NOT NULL)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mis_budget_requests (scope, requested_at) "
                "VALUES ('old', clock_timestamp())"
            )
        )
        await connection.execute(insert(schema_versions).values(version=5))
    await migrate(pg_blank_engine)
    await migrate(pg_blank_engine)
    async with pg_blank_engine.connect() as connection:
        assert list((await connection.execute(select(schema_versions.c.version))).scalars()) == [
            SCHEMA_VERSION
        ]
        rows = (await connection.execute(select(budget_requests))).mappings().all()
    assert len(rows) == 1 and rows[0]["owner"] is None and rows[0]["hold_until"] is None
    await PostgresBudget(pg_blank_engine, TOKEN).reserve()
