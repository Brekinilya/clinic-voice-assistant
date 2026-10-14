"""Автономная сборка текстового API с явным выбором сценария и хранилища."""

from contextlib import asynccontextmanager
from secrets import token_bytes

import httpx
from sqlalchemy import select

from app.backend import backend_engine
from app.booking.memory import MemoryBookingStore
from app.booking.schedule import JsonSchedule
from app.booking.service import BookingService
from app.cancellation.memory import MemoryCancellationStore
from app.cancellation.service import CancellationService
from app.demo import DemoSchedule, EmulatorBudget, demo_emulator
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.natural import BookingConversation
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.mis.http import HttpMedicalSystem
from app.ports import Clock
from app.rescheduling.memory import MemoryMoveStore
from app.rescheduling.service import RescheduleService
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
        JsonSchedule.load(settings.schedule_path, clock)
        if settings.schedule_path is not None
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
        emulator = demo_emulator(clock)
        async with httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=emulator.transport(),
        ) as client:
            booking = BookingService(
                HttpMedicalSystem(client, EmulatorBudget()),
                booking_store,
                schedule,
                clock,
                require_dialogue_guard=settings.dialogue_mode == "natural",
            )
            safety = RulesSafety(clock) if settings.dialogue_mode == "natural" else None
            conversation = (
                BookingConversation(
                    booking,
                    dialogue_store,
                    safety,
                    cancellation=CancellationService(booking.mis, cancellation_store, clock),
                    rescheduling=RescheduleService(booking, move_store),
                )
                if safety is not None
                else None
            )
            yield DialogueService(
                booking, dialogue_store, key, safety=safety, conversation=conversation
            )
    finally:
        if engine is not None:
            await engine.dispose()
