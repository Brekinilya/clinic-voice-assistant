"""Точка сборки: только здесь выбираем реализации внешних ресурсов."""

from collections.abc import Callable
from dataclasses import dataclass

from app.clock import ClinicClock
from app.ports import Clock, Database
from app.settings import Settings
from app.storage.postgres import PostgresDatabase


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    database: Database
    clock: Clock


def build_runtime(
    settings: Settings,
    database_factory: Callable[[Settings], Database] | None = None,
    clock: Clock | None = None,
) -> Runtime:
    factory = database_factory if database_factory is not None else PostgresDatabase.from_settings
    return Runtime(settings, factory(settings), clock if clock is not None else ClinicClock())
