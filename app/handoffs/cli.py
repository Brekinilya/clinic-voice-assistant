"""Служебная CLI перевода: технический клиент для экстренных заявок без идентификации.

    uv run python -m app.handoffs.cli technical-client --check   # только поиск, без записи
    uv run python -m app.handoffs.cli technical-client           # один раз, до включения T08

Номер из вывода — в HANDOFF_TECHNICAL_CLIENT_ID. Повтор безопасен: стенд по фамилии, имени и
дате рождения отдаёт ту же карточку. ФИО и токен не печатаются, только номер клиента.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date

from sqlalchemy import select

from app.backend import backend_engine
from app.mis.budget import Priority, demand
from app.mis.http import HttpMedicalSystem
from app.mis.models import ClientCreate
from app.settings import Settings
from app.storage.booking import PostgresBudget
from app.storage.schema import SCHEMA_VERSION, schema_versions

# Явно вымышленная карточка: по разъяснению организаторов от 15.10 экстренную заявку без
# идентификации можно привязать к техническому клиенту команды
TECHNICAL = ClientCreate(
    last_name="Срочный",
    first_name="Вызов",
    middle_name="Технический",
    birth_date=date(1900, 1, 1),
    comment="Технический клиент для экстренных заявок без идентификации пациента",
)


async def technical_client(mis, *, check: bool, configured: int | None) -> dict:
    with demand(Priority.BACKGROUND):
        if check:
            found = await mis.clients(
                TECHNICAL.last_name, TECHNICAL.first_name, TECHNICAL.birth_date
            )
            client_id = found[0].id if len(found) == 1 else None
            result = {"technical_client_id": client_id, "matches": len(found)}
        else:
            client_id = (await mis.create_client(TECHNICAL)).id
            result = {"technical_client_id": client_id}
    result["configured"] = configured
    if client_id is not None and configured != client_id:
        result["next"] = f"HANDOFF_TECHNICAL_CLIENT_ID={client_id} в .env"
    return result


async def execute(args, settings: Settings) -> dict:
    engine = backend_engine(settings)
    try:
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
            return await technical_client(
                mis, check=args.check, configured=settings.handoff_technical_client_id
            )
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Перевод на оператора (T08): служебные команды")
    commands = root.add_subparsers(dest="command", required=True)
    item = commands.add_parser(
        "technical-client", help="завести технического клиента для P1 (повтор безопасен)"
    )
    item.add_argument("--check", action="store_true", help="только найти, ничего не создавать")
    return root


def main():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    root = parser()
    args = root.parse_args()
    try:
        result = asyncio.run(execute(args, Settings()))
    except Exception:
        # Диагностика соединения может содержать секреты; печатаем общий код.
        root.exit(1, "Операция не выполнена. Проверьте режим, подключение к базе и миграцию.\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
