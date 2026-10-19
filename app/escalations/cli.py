"""Служебная CLI заявок: технический клиент, журнал и сверка неизвестных POST.

    uv run python -m app.escalations.cli technical-client   # один раз, до первого звонка
    uv run python -m app.escalations.cli status
    uv run python -m app.escalations.cli reconcile

Текст заявок и ФИО не печатаются: только маршрут, приоритет, состояние и номера.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date

from sqlalchemy import select

from app.backend import backend_engine
from app.clock import ClinicClock
from app.escalations.service import EscalationService
from app.escalations.settings import EscalationSettings
from app.mis.budget import Priority, demand
from app.mis.http import HttpMedicalSystem
from app.mis.models import ClientCreate
from app.settings import Settings
from app.storage.booking import PostgresBudget
from app.storage.escalations import PostgresEscalationStore
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


async def execute(args, settings: Settings) -> dict:
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        store = PostgresEscalationStore(engine)
        if args.command == "status":
            items = await store.recent(args.limit)
            return {
                "shown": len(items),
                "requests": [
                    {
                        "route": r.route,
                        "priority": r.priority,
                        "state": r.state,
                        "technical": r.technical,
                        "escalation_id": r.escalation_id,
                        "reason": r.reason,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in items
                ],
            }
        async with HttpMedicalSystem.live_client(settings) as client:
            mis = HttpMedicalSystem(
                client,
                PostgresBudget(
                    engine, settings.mis_token.get_secret_value(), settings.mis_rate_limit_per_min
                ),
            )
            if args.command == "technical-client":
                with demand(Priority.BACKGROUND):
                    created = await mis.create_client(TECHNICAL)
                return {
                    "technical_client_id": created.id,
                    "next": f"ESCALATIONS_TECHNICAL_CLIENT_ID={created.id} в .env",
                }
            service = EscalationService(
                mis,
                store,
                ClinicClock(),
                technical_client_id=EscalationSettings().technical_client_id,
            )
            return {"settled": await service.reconcile()}
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Заявки на соединение со специалистом (T08)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("technical-client", help="завести технического клиента (повтор безопасен)")
    status = commands.add_parser("status", help="последние заявки без текста и ФИО")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1..200")
    commands.add_parser(
        "reconcile", help="сверить неизвестные POST чтением, без повторной отправки"
    )
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
