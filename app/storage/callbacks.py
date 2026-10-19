"""Задачи обратного звонка в PostgreSQL: общие для процессов, переживают перезапуск."""

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.callbacks.models import CallbackTask
from app.storage.schema import callback_tasks as tasks


def _task(row) -> CallbackTask:
    return CallbackTask.model_validate(row._asdict())


class PostgresCallbackStore:
    def __init__(self, engine):
        self.engine = engine

    async def add(self, task):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(tasks).values(**task.model_dump()).on_conflict_do_nothing()
            )
            return _task(
                (await connection.execute(select(tasks).where(tasks.c.id == task.id))).one()
            )

    async def get(self, task_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(tasks).where(tasks.c.id == task_id))).first()
            return None if row is None else _task(row)

    async def move(self, task_id, expected, version, **changes):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    update(tasks)
                    .where(
                        tasks.c.id == task_id,
                        tasks.c.state.in_(sorted(expected)),
                        tasks.c.version == version,
                    )
                    .values(**changes, version=version + 1)
                    .returning(*tasks.c)
                )
            ).first()
            return None if row is None else _task(row)

    async def queue(self, now, limit=50):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(tasks)
                .where(
                    or_(
                        tasks.c.state == "pending",
                        and_(tasks.c.state == "calling", tasks.c.claimed_until <= now),
                    )
                )
                .order_by(tasks.c.due_at, tasks.c.created_at)
                .limit(limit)
            )
            return [_task(row) for row in rows]
