"""Импорт XLSX, проверка индекса и чтение каталога; секреты в вывод не попадают."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.backend import backend_engine
from app.booking.preferences import SlotPreferences
from app.clock import ClinicClock
from app.mis.http import HttpMedicalSystem
from app.mis.ports import MisError
from app.scheduling.bootstrap import load_index, read_document, token_scope
from app.scheduling.importer import import_workbook
from app.scheduling.models import Catalog, ScheduleImportError
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBudget
from app.storage.schema import SCHEMA_VERSION, schema_versions


def publish_catalog(path: Path, catalog: Catalog, *, replace=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".catalog-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(catalog.model_dump_json(indent=2))
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise ScheduleImportError("DESTINATION_EXISTS") from None
    finally:
        temporary.unlink(missing_ok=True)


async def capture_catalog(mis, settings: Settings, clock) -> Catalog:
    if settings.app_mode != AppMode.HACKATHON:
        raise ScheduleImportError("HACKATHON_MODE_REQUIRED")
    doctors = await mis.doctors(fresh=True)
    return Catalog(
        mode="hackathon",
        base_url=str(settings.mis_base_url),
        token_scope=token_scope(settings),
        captured_at=clock.now(),
        doctors=tuple(doctors),
    )


async def fetch_catalog(settings: Settings, destination: Path, *, replace=False):
    if settings.app_mode != AppMode.HACKATHON:
        raise ScheduleImportError("HACKATHON_MODE_REQUIRED")
    if destination.exists() and not replace:
        raise ScheduleImportError("DESTINATION_EXISTS")
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise ScheduleImportError("DATABASE_MIGRATION_REQUIRED")
        # Общий бюджет PostgreSQL учитывает и запросы работающего ассистента.
        async with HttpMedicalSystem.live_client(settings) as client:
            mis = HttpMedicalSystem(
                client,
                PostgresBudget(
                    engine, settings.mis_token.get_secret_value(), settings.mis_rate_limit_per_min
                ),
            )
            catalog = await capture_catalog(mis, settings, ClinicClock())
        publish_catalog(destination, catalog, replace=replace)
        return {"mode": catalog.mode, "doctors": len(catalog.doctors)}
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Расписание клиники: каталог, импорт и поиск")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-doctors", help="прочитать GET /doctors с токеном команды")
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--replace", action="store_true")
    importer = commands.add_parser("import-xlsx", help="проверить книгу и создать SQLite-индекс")
    importer.add_argument("source", type=Path)
    importer.add_argument("--catalog", type=Path, required=True)
    importer.add_argument("--output", type=Path, required=True)
    importer.add_argument("--replace", action="store_true")
    importer.add_argument("--valid-hours", type=int, default=24)
    check = commands.add_parser("check", help="проверить актуальность и область индекса")
    check.add_argument("path", type=Path)
    find = commands.add_parser(
        "find", help="найти свободные слоты без подтверждения допуска пациента"
    )
    find.add_argument("path", type=Path)
    find.add_argument("--specialty", required=True)
    find.add_argument("--date-from")
    find.add_argument("--date-to")
    find.add_argument("--doctor-id", type=int)
    args = parser.parse_args(argv)
    try:
        settings, clock = Settings(), ClinicClock()
        if args.command == "fetch-doctors":
            result = asyncio.run(fetch_catalog(settings, args.output, replace=args.replace))
        elif args.command == "import-xlsx":
            catalog = read_document(args.catalog, Catalog)
            # Проверка области до чтения книги: учебные ID не переносятся на стенд.
            if catalog.mode != settings.app_mode.value or (
                catalog.mode == "hackathon"
                and (
                    catalog.base_url.rstrip("/") != str(settings.mis_base_url).rstrip("/")
                    or catalog.token_scope != token_scope(settings)
                )
            ):
                raise ScheduleImportError("INDEX_SCOPE_MISMATCH")
            result = import_workbook(
                args.source,
                catalog,
                args.output,
                clock.now(),
                replace=args.replace,
                valid_hours=args.valid_hours,
            ).model_dump(mode="json")
        else:
            schedule = load_index(args.path, settings, clock)
            try:
                if args.command == "check":
                    result = schedule.report.model_dump(mode="json")
                else:
                    preferences = SlotPreferences(
                        date_from=args.date_from,
                        date_to=args.date_to,
                        doctor_ids=(args.doctor_id,) if args.doctor_id is not None else (),
                    )
                    slots = schedule.find(args.specialty, preferences, clock.now())
                    result = {
                        "candidate_count": len(slots),
                        "patient_eligibility_checked": False,
                        "first_slots": [s.model_dump(mode="json") for s in slots[:3]],
                    }
            finally:
                schedule.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ScheduleImportError, MisError) as error:
        print(f"Расписание не принято: {error}")
    except Exception:
        # Текст драйвера/JSON может содержать токен, путь или сведения о пациентах.
        print("Расписание не принято: INVALID_INPUT_OR_DEPENDENCY")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
