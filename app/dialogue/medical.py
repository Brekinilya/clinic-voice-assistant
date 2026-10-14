"""Граница медицинских советов: предложение записи, затем R-04 при настоянии."""

from collections.abc import Awaitable, Callable

from app.booking.models import ActionState
from app.booking.service import BookingService
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.dialogue.ports import DialogueStore
from app.voice.phrases import load_phrasebook

TransferHandler = Callable[[DialogueState, Turn, str], Awaitable[tuple[DialogueState, Reply]]]


class MedicalConversation:
    def __init__(
        self, booking: BookingService, store: DialogueStore, transfer: TransferHandler
    ) -> None:
        self.booking, self.store, self.transfer = booking, store, transfer
        # Общий рендер словоформ подключится отдельным PR speech-render.
        # Здесь предложение фиксировано: терапевт, без подстановки симптомов или советов.
        phrases = load_phrasebook()
        self.text = phrases.get("medical.boundary").text.replace("{specialty:datv}", "терапевту")
        self.goodbye = phrases.get("closing.goodbye").text

    def prompt(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        return state, reply(state, turn.turn_id, self.text, code="MEDICAL_BOUNDARY")

    async def question(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        if state.medical_boundary_pending:
            return await self.transfer(state, turn, "R-04")
        await self.booking.store.release_call_holds(state.call_id)
        self.booking.clear_call_cache(state.call_id)
        preserve_action = state.phase == Phase.RECONCILING
        if state.proposal is not None:
            action = await self.booking.store.get(state.proposal.action_id)
            preserve_action |= action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }
        changes = {
            "medical_boundary_pending": True,
            "authorization": None,
            "options": (),
            "visit_items": (),
            "visit_cursor": None,
            "visit_has_more": False,
            "misunderstandings": 0,
        }
        if preserve_action:
            # Граничная фраза не стирает журнал уже отправленной операции.
            changes["phase"] = Phase.RECONCILING
        else:
            changes.update(
                phase=Phase.IDENTIFIED if state.client_id is not None else Phase.READY,
                proposal=None,
            )
        return self.prompt(state.model_copy(update=changes), turn)

    async def decline(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        if state.phase == Phase.RECONCILING:
            return await self.transfer(state, turn, "R-08")
        state = await self.store.end(state.call_id)
        return state, reply(state, turn.turn_id, self.goodbye, code="CALL_ENDED")
