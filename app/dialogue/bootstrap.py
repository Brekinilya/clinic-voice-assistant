"""Автономная сборка текстового API с явным выбором сценария и хранилища."""

from contextlib import asynccontextmanager
from secrets import token_bytes

import httpx
from sqlalchemy import select

from app.backend import backend_engine
from app.booking.memory import MemoryBookingStore
from app.cancellation.memory import MemoryCancellationStore
from app.demo import DemoSchedule, EmulatorBudget, demo_emulator
from app.dialogue.composition import assemble_dialogue
from app.dialogue.memory import MemoryDialogueStore
from app.mis.http import HttpMedicalSystem
from app.ports import Clock
from app.rescheduling.memory import MemoryMoveStore
from app.scheduling.bootstrap import configured_schedule
from app.scheduling.index import SqliteSchedule
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBookingStore
from app.storage.cancellation import PostgresCancellationStore
from app.storage.dialogue import PostgresDialogueStore
from app.storage.rescheduling import PostgresMoveStore
from app.storage.schema import SCHEMA_VERSION, schema_versions


@asynccontextmanager
async def open_command_backend(settings: Settings, clock: Clock):
    if not settings.backend_demo_enabled or settings.app_mode != AppMode.OFFLINE_TEST:
        raise ValueError("автономный HTTP-стенд не включён")
    schedule = (
        configured_schedule(settings, clock)
        if settings.schedule_path is not None or settings.schedule_index_path is not None
        else DemoSchedule(clock.now())
    )
    engine = None
    try:
        if settings.dialogue_storage == "postgres":
            engine = backend_engine(settings)
            async with engine.connect() as connection:
                versions = list(
                    (await connection.execute(select(schema_versions.c.version))).scalars()
                )
                if versions != [SCHEMA_VERSION]:
                    raise RuntimeError("сначала примените миграцию backend")
            booking_store = PostgresBookingStore(engine)
            dialogue_store = PostgresDialogueStore(engine)
            cancellation_store = PostgresCancellationStore(engine)
            move_store = PostgresMoveStore(engine)
            key = settings.dialogue_hash_key.get_secret_value().encode()
        else:
            dialogue_store = MemoryDialogueStore()
            booking_store = MemoryBookingStore(guard_reader=dialogue_store)
            cancellation_store = MemoryCancellationStore(booking_store.ledger, dialogue_store)
            move_store = MemoryMoveStore(booking_store.ledger, dialogue_store)
            key = token_bytes(32)
        emulator = demo_emulator(
            clock, doctors=schedule.doctors if isinstance(schedule, SqliteSchedule) else None
        )
        async with httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=emulator.transport(),
        ) as client:
            yield assemble_dialogue(
                settings,
                clock,
                HttpMedicalSystem(client, EmulatorBudget()),
                schedule,
                booking_store,
                dialogue_store,
                cancellation_store,
                move_store,
                key,
            )
    finally:
        if isinstance(schedule, SqliteSchedule):
            schedule.close()
        if engine is not None:
            await engine.dispose()


@asynccontextmanager
async def open_dialogue_backend(settings: Settings, clock: Clock):
    if settings.app_mode == AppMode.HACKATHON:
        from app.dialogue.live import open_live_backend

        async with open_live_backend(settings, clock) as service:
            yield service
    else:
        async with open_command_backend(settings, clock) as service:
            yield service
