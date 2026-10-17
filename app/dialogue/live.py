"""Разговор со стендом: токен команды, проверенное расписание и устойчивые журналы."""

from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import select

from app.backend import backend_engine
from app.dialogue.composition import assemble_dialogue
from app.mis.budget import Priority, demand
from app.mis.http import HttpMedicalSystem
from app.scheduling.bootstrap import configured_schedule
from app.scheduling.index import SqliteSchedule
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBookingStore, PostgresBudget
from app.storage.cancellation import PostgresCancellationStore
from app.storage.dialogue import PostgresDialogueStore
from app.storage.rescheduling import PostgresMoveStore
from app.storage.schema import SCHEMA_VERSION, schema_versions


@asynccontextmanager
async def open_live_backend(settings: Settings, clock):
    if settings.app_mode != AppMode.HACKATHON or not settings.dialogue_enabled:
        raise ValueError("внешний разговорный backend не включён")
    schedule = configured_schedule(settings, clock)
    if not isinstance(schedule, SqliteSchedule):
        raise ValueError("для внешнего диалога нужен индекс расписания с ID стенда")
    engine = None
    try:
        engine = backend_engine(settings)
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        async with HttpMedicalSystem.live_client(settings) as client:
            mis = HttpMedicalSystem(
                client,
                PostgresBudget(
                    engine, settings.mis_token.get_secret_value(), settings.mis_rate_limit_per_min
                ),
            )
            # Проверяем привязку Excel к актуальному справочнику до первого звонка.
            # Сразу после импорта окно может быть занято: ждём его, а не падаем.
            with demand(Priority.BACKGROUND, until=clock.now() + timedelta(seconds=90)):
                current = {doctor.id: doctor for doctor in await mis.doctors(fresh=True)}
            if any(
                doctor.id not in current or not schedule.matches_doctor(current[doctor.id])
                for doctor in schedule.doctors
                if doctor.is_active
            ):
                raise RuntimeError("справочник МИС изменился: повторите импорт расписания")
            yield assemble_dialogue(
                settings,
                clock,
                mis,
                schedule,
                PostgresBookingStore(engine),
                PostgresDialogueStore(engine),
                PostgresCancellationStore(engine),
                PostgresMoveStore(engine),
                settings.dialogue_hash_key.get_secret_value().encode(),
            )
    finally:
        schedule.close()
        if engine is not None:
            await engine.dispose()
