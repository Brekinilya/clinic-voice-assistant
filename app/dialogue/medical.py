"""Граница медицинских советов: предложение записи, затем R-04 при настоянии."""

from collections.abc import Awaitable, Callable

from app.booking.models import ActionState
from app.booking.service import BookingService
from app.cancellation.models import CancelState
from app.cancellation.service import CancellationService
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.dialogue.ports import DialogueStore
from app.rescheduling.models import MoveState
from app.voice.phrases import load_phrasebook

TransferHandler = Callable[[DialogueState, Turn, str], Awaitable[tuple[DialogueState, Reply]]]


class MedicalConversation:
    def __init__(
        self,
        booking: BookingService,
        store: DialogueStore,
        transfer: TransferHandler,
        *,
        cancellation: CancellationService | None = None,
        rescheduling=None,
        release=None,
    ) -> None:
        self.booking, self.store, self.transfer = booking, store, transfer
        self.cancellation = cancellation
        self.rescheduling = rescheduling
        self.release = release if release is not None else booking.store.release_call_holds
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
        await self.release(state.call_id)
        self.booking.clear_call_cache(state.call_id)
        preserve_action = state.phase == Phase.RECONCILING
        if state.proposal is not None:
            action = await self.booking.store.get(state.proposal.action_id)
            preserve_action |= action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }
        preserve_cancel = state.phase == Phase.CANCEL_RECONCILING
        preserve_move = state.phase in {Phase.MOVE_PENDING, Phase.MOVE_FINISH_CONFIRM}
        if state.move_proposal is not None and self.rescheduling is not None:
            move = await self.rescheduling.store.get(state.move_proposal.action_id)
            preserve_move |= move.state not in {
                MoveState.HELD,
                MoveState.ABORTED,
                MoveState.EXPIRED,
            }
        if state.cancel_proposal is not None and self.cancellation is not None:
            cancel = await self.cancellation.store.get(state.cancel_proposal.action_id)
            preserve_cancel |= cancel.state in {
                CancelState.COMMITTING,
                CancelState.RECONCILING,
                CancelState.CANCELLED,
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
        if preserve_move:
            changes["phase"] = Phase.MOVE_PENDING
        elif preserve_cancel:
            changes["phase"] = Phase.CANCEL_RECONCILING
        elif preserve_action:
            # Граничная фраза не стирает журнал уже отправленной операции.
            changes["phase"] = Phase.RECONCILING
        else:
            changes.update(
                phase=Phase.IDENTIFIED if state.client_id is not None else Phase.READY,
                proposal=None,
                cancel_proposal=None,
                cancel_target=None,
                move_proposal=None,
                move_target=None,
            )
        return self.prompt(state.model_copy(update=changes), turn)

    async def decline(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        if state.phase in {
            Phase.RECONCILING,
            Phase.CANCEL_RECONCILING,
            Phase.MOVE_PENDING,
            Phase.MOVE_FINISH_CONFIRM,
        }:
            return await self.transfer(state, turn, "R-08")
        state = await self.store.end(state.call_id)
        return state, reply(state, turn.turn_id, self.goodbye, code="CALL_ENDED")
