"""Локальный просмотр заявок, сохранённых в лист ожидания."""

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select

from app.backend import backend_engine
from app.settings import Settings
from app.storage.schema import SCHEMA_VERSION, schema_versions
from app.storage.waitlist import PostgresWaitlistStore


async def list_requests(limit: int, settings: Settings) -> list[dict]:
    engine = backend_engine(settings)
    try:
        async with engine.connect() as connection:
            versions = list((await connection.execute(select(schema_versions.c.version))).scalars())
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("сначала примените миграцию backend")
        requests = await PostgresWaitlistStore(engine).recent(limit)
        return [
            {
                "request_id": request.request_id,
                "client_id": request.client_id,
                "specialty": request.specialty,
                "preferences": request.preferences.model_dump(mode="json"),
                "created_at": request.created_at.isoformat(),
            }
            for request in requests
        ]
    finally:
        await engine.dispose()


def main() -> None:
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Заявки пациентов в листе ожидания")
    parser.add_argument("list", choices=("list",))
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 501), metavar="1..500")
    args = parser.parse_args()
    try:
        result = asyncio.run(list_requests(args.limit, Settings()))
    except Exception:
        parser.exit(1, "Не удалось прочитать лист ожидания. Проверьте базу и миграцию.\n")
    print(json.dumps({"shown": len(result), "requests": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
