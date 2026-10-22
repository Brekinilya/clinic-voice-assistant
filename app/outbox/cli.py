"""Служебная CLI очереди. По умолчанию доставка отключена; данные пациента не печатаются."""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from contextlib import AsyncExitStack

from sqlalchemy import select

from app.backend import backend_engine
from app.clock import ClinicClock
from app.mis.http import HttpMedicalSystem
from app.outbox.delivery import DisabledDelivery
from app.outbox.models import JobState
from app.outbox.worker import OutboxWorker
from app.settings import AppMode, Settings
from app.storage.booking import PostgresBudget
from app.storage.outbox import PostgresOutboxStore
from app.storage.outbox_delivery import PostgresClientCommentDelivery, PostgresJournalDelivery
from app.storage.reminders import PostgresReminderStore
from app.storage.schema import SCHEMA_VERSION, schema_versions


async def execute(args, settings: Settings):
    if args.demo_journal and settings.app_mode != AppMode.OFFLINE_TEST:
        raise ValueError("демонстрационный журнал разрешён только в offline_test")
    if args.client_comment and settings.app_mode != AppMode.HACKATHON:
        raise ValueError("отметки в MIS доступны только в режиме hackathon")
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        clock, store = ClinicClock(), PostgresOutboxStore(engine)
        async with AsyncExitStack() as stack:
            if args.demo_journal:
                delivery = PostgresJournalDelivery(engine, clock)
            elif args.client_comment and args.command in {"run", "run-once", "reconcile"}:
                http = await stack.enter_async_context(HttpMedicalSystem.live_client(settings))
                delivery = PostgresClientCommentDelivery(
                    HttpMedicalSystem(
                        http,
                        PostgresBudget(
                            engine,
                            settings.mis_token.get_secret_value(),
                            settings.mis_rate_limit_per_min,
                            clock=clock,
                        ),
                    ),
                    PostgresReminderStore(engine),
                    clock,
                )
            else:
                delivery = DisabledDelivery()
            worker = OutboxWorker(store, delivery, clock)
            if args.command == "recover-expired":
                return {"recovered": await store.recover_expired(clock.now())}
            if args.command == "status":
                jobs = await store.list(args.limit, state=getattr(args, "state", None))
                return {
                    "shown": len(jobs),
                    "states": dict(Counter(j.state for j in jobs)),
                    "jobs": [
                        {
                            "event_id": j.event_id,
                            "kind": j.payload.kind,
                            "state": j.state,
                            "attempts": j.attempts,
                            "last_error": j.last_error,
                        }
                        for j in jobs
                    ],
                }
            if args.command == "resume-blocked":
                job = await store.resume_blocked(args.event_id, clock.now())
                return {"event_id": job.event_id, "state": job.state}
            if args.command == "reconcile":
                job = await worker.reconcile(args.event_id)
                return {"event_id": job.event_id, "state": job.state}
            if args.command == "run":
                while True:
                    job = await worker.run_once()
                    if job is None:
                        await asyncio.sleep(1)
            results = []
            for _ in range(args.limit):
                job = await worker.run_once()
                if job is None:
                    break
                results.append(
                    {
                        "event_id": job.event_id,
                        "state": job.state,
                        "mode": None if job.receipt is None else job.receipt.mode,
                    }
                )
            return {"processed": len(results), "results": results}
    finally:
        await engine.dispose()


def main():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Очередь уведомлений после подтверждённой отмены")
    parser.add_argument(
        "--demo-journal",
        action="store_true",
        help="записать демонстрационный результат; SMS не отправляется",
    )
    parser.add_argument(
        "--client-comment",
        action="store_true",
        help="добавлять отметку в комментарий MIS; реальные SMS не отправляются",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "recover-expired", help="пометить просроченные задания unknown без доставки"
    )
    commands.add_parser("run", help="непрерывно обрабатывать очередь")
    for command in ("status", "run-once"):
        item = commands.add_parser(command)
        item.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1..100")
        if command == "status":
            item.add_argument("--state", choices=list(JobState), help="фильтр состояния задания")
    for command in ("resume-blocked", "reconcile"):
        item = commands.add_parser(command)
        item.add_argument("event_id")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args, Settings()))
    except Exception:
        # Диагностика внешнего соединения может содержать секреты; CLI печатает общий код.
        parser.exit(
            1, "Операция очереди не выполнена. Проверьте режим, миграцию и состояние задания.\n"
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
