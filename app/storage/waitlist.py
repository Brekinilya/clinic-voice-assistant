"""PostgreSQL-хранилище заявок листа ожидания."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.outbox.models import waitlist_sms_event
from app.storage.journal import journal_transaction
from app.storage.schema import outbox_jobs, waitlist_requests
from app.waitlist.models import WaitlistRequest


class PostgresWaitlistStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def create(self, request: WaitlistRequest) -> WaitlistRequest:
        async with journal_transaction(self.engine) as connection:
            result = await connection.execute(
                insert(waitlist_requests)
                .values(
                    request_id=request.request_id,
                    call_id=request.call_id,
                    turn_id=request.turn_id,
                    client_id=request.client_id,
                    specialty=request.specialty,
                    preferences=request.preferences.model_dump(mode="json"),
                    created_at=request.created_at,
                )
                .on_conflict_do_nothing(index_elements=[waitlist_requests.c.request_id])
            )
            if result.rowcount == 1:
                from app.storage.outbox import values as outbox_values

                event = waitlist_sms_event(
                    request.request_id, request.client_id, request.created_at
                )
                await connection.execute(
                    insert(outbox_jobs)
                    .values(**outbox_values(event))
                    .on_conflict_do_nothing(
                        index_elements=[outbox_jobs.c.source_id, outbox_jobs.c.kind]
                    )
                )
            row = await connection.execute(
                select(waitlist_requests).where(
                    waitlist_requests.c.request_id == request.request_id
                )
            )
            decoded = _decode(row.mappings().one())
            candidate = request.model_copy(update={"created_at": decoded.created_at})
            if decoded != candidate:
                raise ValueError("WAITLIST_REQUEST_CONFLICT")
            return decoded

    async def by_call(self, call_id: str) -> tuple[WaitlistRequest, ...]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(waitlist_requests)
                .where(waitlist_requests.c.call_id == call_id)
                .order_by(waitlist_requests.c.created_at, waitlist_requests.c.turn_id)
            )
            return tuple(_decode(row) for row in rows.mappings())

    async def recent(self, limit: int = 100) -> tuple[WaitlistRequest, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("лимит заявок должен быть от 1 до 500")
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(waitlist_requests)
                .order_by(waitlist_requests.c.created_at.desc(), waitlist_requests.c.turn_id.desc())
                .limit(limit)
            )
            return tuple(_decode(row) for row in rows.mappings())


def _decode(row) -> WaitlistRequest:
    return WaitlistRequest(
        request_id=row["request_id"],
        call_id=row["call_id"],
        turn_id=row["turn_id"],
        client_id=row["client_id"],
        specialty=row["specialty"],
        preferences=row["preferences"],
        created_at=row["created_at"],
    )
