"""Журнал заявок в PostgreSQL: общий для процессов, переживает перезапуск."""

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.escalations.models import EscalationRecord
from app.storage.schema import escalation_requests as records


def _record(row) -> EscalationRecord:
    return EscalationRecord.model_validate(row._asdict())


class PostgresEscalationStore:
    def __init__(self, engine):
        self.engine = engine

    async def prepare(self, record):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(records).values(**record.model_dump()).on_conflict_do_nothing()
            )
            row = await connection.execute(select(records).where(records.c.id == record.id))
            return _record(row.one())

    async def get(self, record_id):
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(select(records).where(records.c.id == record_id))
            ).first()
            return None if row is None else _record(row)

    async def move(self, record_id, expected, **changes):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(records)
                    .where(records.c.id == record_id, records.c.state.in_(sorted(expected)))
                    .values(**changes)
                    .returning(*records.c)
                )
            ).first()
            return None if row is None else _record(row)

    async def due_checks(self, now):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(records).where(records.c.state == "unknown", records.c.next_check_at <= now)
            )
            return [_record(row) for row in rows]

    async def recent(self, limit=50):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(records).order_by(records.c.created_at.desc()).limit(limit)
            )
            return [_record(row) for row in rows]
