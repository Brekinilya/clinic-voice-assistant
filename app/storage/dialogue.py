"""Короткая транзакция на квитанцию; HTTP и подготовка брони идут между транзакциями."""

from contextlib import asynccontextmanager

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.dialogue.models import Claim, DialogueError, DialogueState, Phase, Reply, reply
from app.storage.schema import dialogue_receipts as receipts
from app.storage.schema import dialogue_sessions as sessions


class PostgresDialogueStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def _locked(self, call_id: str):
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(sessions)
                .values(
                    call_id=call_id, state=DialogueState(call_id=call_id).model_dump(mode="json")
                )
                .on_conflict_do_nothing()
            )
            row = (
                await connection.execute(
                    select(sessions).where(sessions.c.call_id == call_id).with_for_update()
                )
            ).one()
            yield connection, row

    async def inspect(self, call_id: str) -> DialogueState:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(sessions.c.state).where(sessions.c.call_id == call_id)
                )
            ).scalar_one_or_none()
            return (
                DialogueState(call_id=call_id)
                if payload is None
                else DialogueState.model_validate(payload)
            )

    async def claim(self, call_id: str, turn_id: int, digest: str) -> Claim:
        async with self._locked(call_id) as (connection, row):
            state = DialogueState.model_validate(row.state)
            receipt = (
                await connection.execute(
                    select(receipts).where(
                        receipts.c.call_id == call_id, receipts.c.turn_id == turn_id
                    )
                )
            ).first()
            if receipt is not None:
                if receipt.digest != digest:
                    raise DialogueError("TURN_CONTENT_CONFLICT")
                cached = None if receipt.reply is None else Reply.model_validate(receipt.reply)
                return Claim(state=state, acquired=False, cached=cached)
            if state.phase == Phase.ENDED:
                raise DialogueError("CALL_ENDED")
            if row.pending_turn is not None:
                raise DialogueError("TURN_IN_PROGRESS")
            if turn_id != state.last_turn_id + 1:
                raise DialogueError("TURN_OUT_OF_ORDER")
            state = state.model_copy(
                update={"version": state.version + 1, "pending_turn_id": turn_id}
            )
            await connection.execute(
                insert(receipts).values(call_id=call_id, turn_id=turn_id, digest=digest)
            )
            await connection.execute(
                update(sessions)
                .where(sessions.c.call_id == call_id)
                .values(state=state.model_dump(mode="json"), pending_turn=turn_id)
            )
            return Claim(state=state, acquired=True)

    async def complete(self, state: DialogueState, turn_id: int, result: Reply) -> bool:
        async with self._locked(state.call_id) as (connection, row):
            current = DialogueState.model_validate(row.state)
            if (
                current.phase == Phase.ENDED
                or current.version != state.version
                or row.pending_turn != turn_id
            ):
                return False
            state = state.model_copy(update={"last_turn_id": turn_id, "pending_turn_id": None})
            await connection.execute(
                update(sessions)
                .where(sessions.c.call_id == state.call_id)
                .values(state=state.model_dump(mode="json"), pending_turn=None)
            )
            await connection.execute(
                update(receipts)
                .where(receipts.c.call_id == state.call_id, receipts.c.turn_id == turn_id)
                .values(reply=result.model_dump(mode="json"))
            )
            return True

    async def end(self, call_id: str) -> DialogueState:
        async with self._locked(call_id) as (connection, row):
            state = DialogueState.model_validate(row.state)
            if state.phase == Phase.ENDED:
                return state
            state = state.model_copy(
                update={
                    "phase": Phase.ENDED,
                    "version": state.version + 1,
                    "last_turn_id": row.pending_turn or state.last_turn_id,
                    "pending_turn_id": None,
                    "client_id": None,
                    "proposal": None,
                    "options": (),
                    "retry_at": None,
                }
            )
            await connection.execute(
                update(sessions)
                .where(sessions.c.call_id == call_id)
                .values(state=state.model_dump(mode="json"), pending_turn=None)
            )
            if row.pending_turn is not None:
                result = reply(state, row.pending_turn, "Разговор завершён.", code="CALL_ENDED")
                await connection.execute(
                    update(receipts)
                    .where(receipts.c.call_id == call_id, receipts.c.turn_id == row.pending_turn)
                    .values(reply=result.model_dump(mode="json"))
                )
            return state
