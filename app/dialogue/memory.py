"""Сохранение в памяти для автономного запуска; контракт тот же, что у PostgreSQL."""

import asyncio

from app.dialogue.models import Claim, DialogueError, DialogueState, Phase, Receipt, Reply, reply


class MemoryDialogueStore:
    def __init__(self) -> None:
        self.states: dict[str, DialogueState] = {}
        self.receipts: dict[tuple[str, int], Receipt] = {}
        self.pending: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def inspect(self, call_id: str) -> DialogueState:
        async with self._lock:
            return self.states.get(call_id, DialogueState(call_id=call_id))

    async def claim(self, call_id: str, turn_id: int, digest: str) -> Claim:
        async with self._lock:
            state = self.states.get(call_id, DialogueState(call_id=call_id))
            receipt = self.receipts.get((call_id, turn_id))
            if receipt is not None:
                if receipt.digest != digest:
                    raise DialogueError("TURN_CONTENT_CONFLICT")
                return Claim(state=state, acquired=False, cached=receipt.reply)
            if state.phase == Phase.ENDED:
                raise DialogueError("CALL_ENDED")
            if call_id in self.pending:
                raise DialogueError("TURN_IN_PROGRESS")
            if turn_id != state.last_turn_id + 1:
                raise DialogueError("TURN_OUT_OF_ORDER")
            state = state.model_copy(
                update={"version": state.version + 1, "pending_turn_id": turn_id}
            )
            self.states[call_id] = state
            self.pending[call_id] = turn_id
            self.receipts[(call_id, turn_id)] = Receipt(digest=digest)
            return Claim(state=state, acquired=True)

    async def complete(self, state: DialogueState, turn_id: int, result: Reply) -> bool:
        async with self._lock:
            current = self.states[state.call_id]
            if (
                current.phase == Phase.ENDED
                or current.version != state.version
                or self.pending.get(state.call_id) != turn_id
            ):
                return False
            self.states[state.call_id] = state.model_copy(
                update={"last_turn_id": turn_id, "pending_turn_id": None}
            )
            old = self.receipts[(state.call_id, turn_id)]
            self.receipts[(state.call_id, turn_id)] = old.model_copy(update={"reply": result})
            del self.pending[state.call_id]
            return True

    async def end(self, call_id: str) -> DialogueState:
        async with self._lock:
            state = self.states.get(call_id, DialogueState(call_id=call_id))
            if state.phase == Phase.ENDED:
                return state
            pending = self.pending.pop(call_id, None)
            state = state.model_copy(
                update={
                    "phase": Phase.ENDED,
                    "version": state.version + 1,
                    "last_turn_id": pending or state.last_turn_id,
                    "pending_turn_id": None,
                    "client_id": None,
                    "proposal": None,
                    "options": (),
                    "retry_at": None,
                }
            )
            self.states[call_id] = state
            if pending is not None:
                old = self.receipts[(call_id, pending)]
                self.receipts[(call_id, pending)] = old.model_copy(
                    update={"reply": reply(state, pending, "Разговор завершён.", code="CALL_ENDED")}
                )
            return state
