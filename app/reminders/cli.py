"""Служебная CLI напоминаний: контакт тестового пациента и состояние заданий.

    uv run python -m app.reminders.cli contact-set --client-id 3
    uv run python -m app.reminders.cli contact-show --client-id 3
    uv run python -m app.reminders.cli jobs --day 2026-10-19

Номер и ФИО не печатаются: только идентификаторы, состояния и технические причины.
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import date

from sqlalchemy import select

from app.backend import backend_engine
from app.reminders.models import Contact, JobState
from app.reminders.ports import ReminderStore
from app.reminders.settings import ReminderSettings
from app.settings import Settings
from app.storage.reminders import PostgresReminderStore
from app.storage.schema import SCHEMA_VERSION, schema_versions

STATES = list(JobState.__args__)


async def run(args, store: ReminderStore, reminders: ReminderSettings) -> dict:
    if args.command == "contact-set":
        endpoint = args.endpoint or reminders.allowed_endpoints[0]
        # Номер вне белого списка не сохраняем: звонить на него всё равно нельзя
        if endpoint not in reminders.allowed_endpoints:
            raise ValueError("адрес не входит в REMINDERS_ALLOWED_ENDPOINTS")
        await store.save_contact(Contact(client_id=args.client_id, endpoint=endpoint))
        contact = await store.contact(args.client_id)
        return {
            "client_id": args.client_id,
            "saved": True,
            "opt_out": contact.opt_out_at is not None,
        }
    if args.command == "contact-show":
        contact = await store.contact(args.client_id)
        return {
            "client_id": args.client_id,
            "has_contact": contact is not None,
            "endpoint_allowed": contact is not None
            and contact.endpoint in reminders.allowed_endpoints,
            "opt_out_at": None
            if contact is None or contact.opt_out_at is None
            else contact.opt_out_at.isoformat(),
        }
    jobs = await store.list_jobs(args.day, args.state, args.limit)
    return {
        "shown": len(jobs),
        "states": dict(Counter(j.state for j in jobs)),
        "jobs": [
            {
                "job_id": j.id,
                "visit_id": j.visit_id,
                "scheduled_at": j.scheduled_at.isoformat(),
                "call_day": j.call_day.isoformat(),
                "state": j.state,
                "attempts": j.attempts,
                "next_at": j.next_at.isoformat(),
                "outcome": j.outcome,
                "reason": j.reason,
                "sms_pending": j.sms_pending,
            }
            for j in jobs
        ],
    }


async def execute(args, settings: Settings, reminders: ReminderSettings) -> dict:
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        return await run(args, PostgresReminderStore(engine), reminders)
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Напоминания С-10: контакты и задания обзвона")
    commands = root.add_subparsers(dest="command", required=True)
    for name, text in (
        ("contact-set", "завести контакт тестовому пациенту (номер из белого списка)"),
        ("contact-show", "есть ли контакт и отказ от обзвона; номер не печатается"),
    ):
        item = commands.add_parser(name, help=text)
        item.add_argument("--client-id", type=int, required=True)
        if name == "contact-set":
            item.add_argument("--endpoint", help="по умолчанию — первый из белого списка")
    item = commands.add_parser("jobs", help="задания напоминаний и их исходы")
    item.add_argument("--day", type=date.fromisoformat, help="день звонка, ГГГГ-ММ-ДД")
    item.add_argument("--state", choices=STATES)
    item.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="1..200")
    return root


def main():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    root = parser()
    args = root.parse_args()
    try:
        result = asyncio.run(execute(args, Settings(), ReminderSettings()))
    except ValueError as error:
        root.exit(1, f"{error}\n")
    except Exception:
        # Диагностика соединения может содержать секреты; печатаем общий код.
        root.exit(1, "Операция не выполнена. Проверьте подключение к базе и миграцию.\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
