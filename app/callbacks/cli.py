"""Очередь обратных звонков для сотрудника регистратуры.

    uv run python -m app.callbacks.cli queue                    # ждущие по сроку
    uv run python -m app.callbacks.cli claim <id> --owner <кто>  # взять задачу
    uv run python -m app.callbacks.cli result <id> --owner <кто> --answered | --no-answer

Карточка перевода — в заявке МИС (escalation_ref, app.escalations.cli status). ФИО и номер
здесь не печатаются.
"""

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select

from app.backend import backend_engine
from app.callbacks.service import CallbackService
from app.clock import ClinicClock
from app.reminders.calendar import ClinicCalendar
from app.settings import Settings
from app.storage.callbacks import PostgresCallbackStore
from app.storage.schema import SCHEMA_VERSION, schema_versions


def view(task, now) -> dict:
    return {
        "id": task.id,
        "route": task.route,
        "priority": task.priority,
        "state": task.state,
        "due_at": task.due_at.isoformat(),
        "overdue": task.due_at < now,
        "attempts": task.attempts,
        "owner": task.owner,
        "escalation_ref": task.escalation_ref,
        "preferred": task.preferred,
        "sms_pending": task.sms_pending,
    }


async def run(args, service: CallbackService) -> dict:
    now = service.clock.now()
    if args.command == "queue":
        items = await service.queue(args.limit)
        return {"shown": len(items), "tasks": [view(t, now) for t in items]}
    if args.command == "claim":
        task = await service.claim(args.id, args.owner)
    else:
        task = await service.result(args.id, args.owner, args.answered)
    if task is None:
        raise ValueError("задача не найдена, уже взята другим или результат уже отмечен")
    return view(task, now)


async def execute(args, settings: Settings) -> dict:
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        service = CallbackService(
            PostgresCallbackStore(engine), ClinicClock(), ClinicCalendar.load().holidays
        )
        return await run(args, service)
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Обратные звонки (регламент §7.6)")
    commands = root.add_subparsers(dest="command", required=True)
    queue = commands.add_parser("queue", help="ждущие задачи по сроку")
    queue.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1..200")
    for name in ("claim", "result"):
        item = commands.add_parser(name)
        item.add_argument("id")
        item.add_argument("--owner", required=True, help="кто звонит (логин сотрудника)")
        if name == "result":
            outcome = item.add_mutually_exclusive_group(required=True)
            outcome.add_argument("--answered", dest="answered", action="store_true")
            outcome.add_argument("--no-answer", dest="answered", action="store_false")
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
    except ValueError as error:
        root.exit(1, f"{error}\n")
    except Exception:
        root.exit(1, "Операция не выполнена. Проверьте подключение к базе и миграцию.\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
