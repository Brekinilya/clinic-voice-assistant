"""python -m app.recovery once|watch|status. Сверка не отправляет изменения МИС."""

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select

from app.backend import backend_engine
from app.clock import ClinicClock
from app.mis.http import HttpMedicalSystem
from app.recovery.worker import RecoveryWorker
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBookingStore, PostgresBudget
from app.storage.cancellation import PostgresCancellationStore
from app.storage.recovery import PostgresRecoveryStore
from app.storage.rescheduling import PostgresMoveStore
from app.storage.schema import SCHEMA_VERSION, schema_versions


def brief(record):
    return {
        key: getattr(record, key) for key in ("kind", "action_id", "state", "attempts", "reason")
    }


async def execute(args, settings):
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала выполните python -m app.backend migrate")
        store, clock = PostgresRecoveryStore(engine), ClinicClock()
        if args.command == "status":
            return {"operations": [brief(r) for r in await store.list(args.limit, args.state)]}
        if settings.app_mode != AppMode.HACKATHON:
            raise ValueError("внешняя сверка запускается только в режиме hackathon")
        async with HttpMedicalSystem.live_client(settings) as client:
            mis = HttpMedicalSystem(
                client,
                PostgresBudget(
                    engine, settings.mis_token.get_secret_value(), settings.mis_rate_limit_per_min
                ),
            )
            worker = RecoveryWorker(
                store,
                mis,
                PostgresBookingStore(engine),
                PostgresCancellationStore(engine),
                PostgresMoveStore(engine),
                clock,
                wait_s=settings.mis_turn_wait_s,
            )
            while True:
                processed = []
                for _ in range(args.limit):
                    result = await worker.run_once()
                    if result is None:
                        break
                    processed.append(brief(result))
                if args.command == "once":
                    return {"processed": processed}
                if processed:
                    print(json.dumps({"processed": processed}, ensure_ascii=False), flush=True)
                await asyncio.sleep(args.interval)
    finally:
        await engine.dispose()


def main():
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="техническая очередь, без данных пациента")
    status.add_argument("--state", choices=["pending", "running", "watching", "manual", "resolved"])
    for command in (status, commands.add_parser("once"), commands.add_parser("watch")):
        command.add_argument(
            "--limit", type=int, choices=range(1, 101), default=1 if command is not status else 100
        )
        command.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("интервал должен быть от 1 до 60 секунд")
    result = asyncio.run(execute(args, Settings()))
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
