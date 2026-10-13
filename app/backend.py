"""Сборка backend для стенда; подключение разговорного API будет после согласования."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.booking.ports import Schedule
from app.booking.schedule import JsonSchedule, ScheduleError
from app.booking.service import BookingService
from app.clock import ClinicClock
from app.mis.http import HttpMedicalSystem
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBookingStore, PostgresBudget, migrate, schema_versions
from app.storage.postgres import connection_url
from app.storage.schema import SCHEMA_VERSION


def backend_engine(settings: Settings):
    return create_async_engine(
        connection_url(settings),
        pool_size=3,
        max_overflow=0,
        pool_pre_ping=True,
        pool_timeout=3,
        hide_parameters=True,
        connect_args={"timeout": 3},
    )


@asynccontextmanager
async def open_backend(settings: Settings, schedule: Schedule):
    if settings.app_mode != AppMode.HACKATHON:
        raise ValueError("для внешней МИС нужен режим hackathon")
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала нужно применить миграцию backend")
        async with HttpMedicalSystem.live_client(settings) as client:
            mis = HttpMedicalSystem(
                client,
                PostgresBudget(
                    engine,
                    settings.mis_token.get_secret_value(),
                    settings.mis_rate_limit_per_min,
                ),
            )
            yield BookingService(
                mis,
                PostgresBookingStore(engine),
                schedule,
                ClinicClock(),
                require_dialogue_guard=True,
            )
    finally:
        await engine.dispose()


async def apply_schema() -> None:
    engine = backend_engine(Settings())
    try:
        await migrate(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Служебные команды backend")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="создать или обновить схему PostgreSQL")
    schedule_command = commands.add_parser(
        "schedule-check", help="проверить документ расписания без МИС"
    )
    schedule_command.add_argument("path", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "migrate":
        asyncio.run(apply_schema())
    else:
        try:
            schedule = JsonSchedule.load(arguments.path, ClinicClock())
        except ScheduleError as error:
            parser.exit(1, f"Расписание не принято: {error}\n")
        print(
            json.dumps(
                {
                    "slots": len(schedule.slots()),
                    "access_facts": len(schedule.document.access),
                    "sha256": schedule.fingerprint,
                },
                ensure_ascii=False,
            )
        )
