"""Сохранение в памяти для автономного запуска; контракт тот же, что у PostgreSQL."""

import asyncio

from app.booking.guard import check_dialogue_guard
from app.booking.models import Approval
from app.cancellation.guard import check_cancel_guard
from app.cancellation.models import CancelAction
from app.dialogue.models import (
    Claim,
    DialogueError,
    DialogueState,
    Phase,
    Receipt,
    Reply,
    TransferRequest,
    reply,
)
from app.dialogue.transitions import (
    authorize_write,
    handoff_reply,
    observe_input,
    preempt_state,
    start_input,
)


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
            if state.phase in {Phase.ENDED, Phase.HANDOFF}:
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
                current.phase in {Phase.ENDED, Phase.HANDOFF}
                or current.version != state.version
                or self.pending.get(state.call_id) != turn_id
            ):
                return False
            stale = current.input_epoch != state.input_epoch
            if stale:
                state = current
                result = reply(
                    state,
                    turn_id,
                    "Поступила новая реплика. Уточните выбор или подтвердите заново.",
                    code="TURN_INTERRUPTED",
                )
            self.states[state.call_id] = state.model_copy(
                update={
                    "last_turn_id": max(turn_id, state.started_turn_id - 1),
                    "pending_turn_id": None,
                }
            )
            old = self.receipts[(state.call_id, turn_id)]
            self.receipts[(state.call_id, turn_id)] = old.model_copy(update={"reply": result})
            del self.pending[state.call_id]
            return not stale

    async def end(self, call_id: str) -> DialogueState:
        async with self._lock:
            state = self.states.get(call_id, DialogueState(call_id=call_id))
            if state.phase == Phase.ENDED:
                return state
            pending = self.pending.pop(call_id, None)
            state = state.model_copy(
                update={
                    "phase": Phase.ENDED,
                    "identity_draft": None,
                    "version": state.version + 1,
                    "last_turn_id": pending or state.last_turn_id,
                    "pending_turn_id": None,
                    "client_id": None,
                    "proposal": None,
                    "cancel_proposal": None,
                    "cancel_target": None,
                    "move_proposal": None,
                    "move_target": None,
                    "options": (),
                    "retry_at": None,
                    "mis_wait_since": None,
                    "authorization": None,
                    "visit_items": (),
                    "visit_cursor": None,
                    "visit_has_more": False,
                    "query_for_self": False,
                }
            )
            self.states[call_id] = state
            if pending is not None:
                old = self.receipts[(call_id, pending)]
                self.receipts[(call_id, pending)] = old.model_copy(
                    update={"reply": reply(state, pending, "Разговор завершён.", code="CALL_ENDED")}
                )
            return state

    async def observe(self, call_id: str, turn_id: int, digest: str) -> DialogueState:
        async with self._lock:
            state = observe_input(
                self.states.get(call_id, DialogueState(call_id=call_id)), turn_id, digest
            )
            self.states[call_id] = state
            return state

    async def start_input(self, call_id: str, turn_id: int) -> DialogueState:
        async with self._lock:
            state = start_input(self.states.get(call_id, DialogueState(call_id=call_id)), turn_id)
            self.states[call_id] = state
            return state

    async def authorize(self, call_id: str, version: int, epoch: int, turn_id: int) -> bool:
        async with self._lock:
            state = authorize_write(self.states[call_id], version, epoch, turn_id)
            if state is None:
                return False
            self.states[call_id] = state
            return True

    async def check_guard(self, approval: Approval) -> None:
        async with self._lock:
            state = self.states.get(approval.call_id)
            check_dialogue_guard(None if state is None else state.model_dump(mode="json"), approval)

    async def check_cancel_guard(self, approval: Approval, action: CancelAction) -> None:
        async with self._lock:
            state = self.states.get(approval.call_id)
            check_cancel_guard(
                None if state is None else state.model_dump(mode="json"), approval, action
            )

    async def check_move_guard(self, approval, action):
        from app.rescheduling.guard import check_move_guard

        async with self._lock:
            state = self.states.get(approval.call_id)
            check_move_guard(
                None if state is None else state.model_dump(mode="json"), approval, action
            )

    async def preempt(self, call_id: str, turn_id: int, transfer: TransferRequest) -> DialogueState:
        async with self._lock:
            state = self.states.get(call_id, DialogueState(call_id=call_id))
            if turn_id < state.observed_turn_id or state.phase in {Phase.ENDED, Phase.HANDOFF}:
                return state
            pending = self.pending.pop(call_id, None)
            state = preempt_state(state, transfer)
            self.states[call_id] = state
            if pending is not None:
                old = self.receipts[(call_id, pending)]
                self.receipts[(call_id, pending)] = old.model_copy(
                    update={"reply": handoff_reply(state, pending)}
                )
            return state
