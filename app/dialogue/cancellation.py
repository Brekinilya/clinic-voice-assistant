"""С-04: свои посещения, выбор ID, однократное предложение переноса и подтверждение."""

from collections.abc import Awaitable, Callable

from app.booking.models import Approval, BookingError
from app.booking.service import BookingService
from app.cancellation.models import CancelProposal, CancelState, summary
from app.cancellation.service import CancellationService
from app.contracts import NluResult
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.dialogue.ports import DialogueStore
from app.dialogue.visit_text import appointment_text
from app.dialogue.visits import query_from_slots
from app.visits.models import VisitSummary
from app.visits.service import VisitService

TransferHandler = Callable[[DialogueState, Turn, str], Awaitable[tuple[DialogueState, Reply]]]


class CancellationConversation:
    def __init__(
        self,
        booking: BookingService,
        cancellation: CancellationService,
        store: DialogueStore,
        transfer: TransferHandler,
    ) -> None:
        self.booking, self.cancellation, self.store, self.transfer = (
            booking,
            cancellation,
            store,
            transfer,
        )
        self.visits = VisitService(booking.mis, booking.clock)

    @staticmethod
    def visit_phrase(v: VisitSummary) -> str:
        day, at = appointment_text(v.scheduled_at)
        return f"{v.specialty}, {v.doctor_name}: {day}, {at}."

    async def start(self, state: DialogueState, turn: Turn, result: NluResult):
        if state.phase in {Phase.RECONCILING, Phase.CANCEL_RECONCILING}:
            return await self.transfer(state, turn, "R-08")
        await self.booking.store.release_call_holds(state.call_id)
        await self.cancellation.store.release_call(state.call_id)
        state = state.model_copy(
            update={
                "purpose": "cancel",
                "phase": Phase.IDENTIFIED if state.client_id else Phase.IDENTIFYING,
                "options": (),
                "proposal": None,
                "cancel_proposal": None,
                "cancel_target": None,
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "authorization": None,
                "visit_query": query_from_slots(result.slots),
                "cancel_reschedule_offered": False,
                "identity_attempts": 0,
                "misunderstandings": 0,
                "query_for_self": True,
                "visit_id": None,
            }
        )
        return await self.identify_or_list(state, turn)

    async def identify_or_list(self, state: DialogueState, turn: Turn, *, next_page=False):
        if state.client_id is None:
            return state.model_copy(update={"phase": Phase.IDENTIFYING}), reply(
                state.model_copy(update={"phase": Phase.IDENTIFYING}),
                turn.turn_id,
                "Назовите фамилию, имя, отчество и дату рождения.",
                code="IDENTIFICATION_REQUIRED",
            )
        page = await self.visits.page(
            state.client_id, state.visit_query, after=state.visit_cursor if next_page else None
        )
        state = state.model_copy(
            update={
                "visit_items": page.items,
                "visit_cursor": page.cursor,
                "visit_has_more": page.has_more,
                "cancel_proposal": None,
                "cancel_target": None,
                "authorization": None,
            }
        )
        if not page.items:
            state = state.model_copy(update={"phase": Phase.CANCELLED})
            return state, reply(
                state,
                turn.turn_id,
                "Подходящих будущих записей для отмены нет.",
                code="NO_ACTIVE_VISITS",
            )
        if len(page.items) == 1 and not page.has_more:
            return await self.choose(state, turn, page.items[0])
        state = state.model_copy(update={"phase": Phase.CANCEL_CHOOSING})
        phrases = tuple(f"{i}. {self.visit_phrase(v)}" for i, v in enumerate(page.items, 1))
        return state, reply(
            state,
            turn.turn_id,
            *phrases,
            "Какую запись отменить?",
            "Для следующей страницы скажите: дальше."
            if page.has_more
            else "Назовите вариант или специальность.",
            code="CANCEL_CHOICE_REQUIRED",
        )

    async def choose(self, state: DialogueState, turn: Turn, target: VisitSummary):
        state = state.model_copy(
            update={
                "cancel_target": target,
                "visit_items": (),
                "visit_has_more": False,
                "authorization": None,
            }
        )
        if state.cancel_reschedule_offered:
            return await self.prepare(state, turn)
        state = state.model_copy(
            update={"phase": Phase.CANCEL_OFFER, "cancel_reschedule_offered": True}
        )
        return state, reply(
            state,
            turn.turn_id,
            self.visit_phrase(target),
            "Могу перенести запись вместо отмены. Перенести или отменить?",
            code="RESCHEDULE_OFFERED",
        )

    async def prepare(self, state: DialogueState, turn: Turn):
        text = turn.text.casefold()
        reason = (
            "not_needed"
            if "не нужно" in text or "не требуется" in text or "выздоровел" in text
            else "cannot_attend"
            if "не приду" in text
            else None
        )
        action = await self.cancellation.prepare(
            state.call_id, state.client_id, state.cancel_target, reason
        )
        state = state.model_copy(
            update={
                "phase": Phase.CANCEL_CONFIRM,
                "cancel_proposal": CancelProposal(
                    action_id=action.action_id,
                    revision=action.revision,
                    visit=summary(action.before),
                    expires_at=action.expires_at,
                ),
                "misunderstandings": 0,
                "authorization": None,
            }
        )
        return self.confirmation(state, turn)

    def confirmation(self, state: DialogueState, turn: Turn):
        return state, reply(
            state,
            turn.turn_id,
            "Подтвердите отмену этой записи:",
            self.visit_phrase(state.cancel_proposal.visit),
            "Отменить её?",
            code="CANCEL_CONFIRMATION_REQUIRED",
        )

    def outcome(self, state: DialogueState, turn: Turn, action):
        if action.state == CancelState.CANCELLED:
            state = state.model_copy(
                update={
                    "phase": Phase.CANCELLED,
                    "cancel_proposal": None,
                    "cancel_target": None,
                    "authorization": None,
                    "visit_items": (),
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Готово, запись отменена. Спасибо, что предупредили.",
                "Могу ещё чем-то помочь?",
                code="CANCELLED",
            )
        if action.state in {CancelState.COMMITTING, CancelState.RECONCILING}:
            state = state.model_copy(
                update={"phase": Phase.CANCEL_RECONCILING, "authorization": None}
            )
            return state, reply(
                state,
                turn.turn_id,
                "Результат отмены уточняется. Для проверки скажите: проверьте отмену.",
                code="CANCEL_RECONCILING",
            )
        state = state.model_copy(
            update={"phase": Phase.CANCEL_CHOOSING, "cancel_proposal": None, "authorization": None}
        )
        return state, reply(
            state,
            turn.turn_id,
            "МИС отклонила отмену. Уточните запись или попросите сотрудника.",
            code="CANCEL_REJECTED",
        )

    async def step(self, state: DialogueState, turn: Turn, result: NluResult):
        intents, slots = set(result.intents), result.slots
        if state.cancel_proposal is not None and state.phase == Phase.CANCEL_CONFIRM:
            action = await self.cancellation.store.get(state.cancel_proposal.action_id)
            if action.state in {
                CancelState.COMMITTING,
                CancelState.RECONCILING,
                CancelState.CANCELLED,
            }:
                state = state.model_copy(update={"phase": Phase.CANCEL_RECONCILING})
            elif action.expires_at <= self.booking.clock.now():
                await self.cancellation.store.release_call(state.call_id)
                state = state.model_copy(
                    update={"phase": Phase.CANCEL_CHOOSING, "cancel_proposal": None}
                )
                return await self.identify_or_list(state, turn)
        if state.phase == Phase.CANCEL_RECONCILING:
            if "I-04" in intents or "проверь" in turn.text.casefold() or "repeat" in intents:
                action = await self.cancellation.reconcile(
                    state.cancel_proposal.action_id, state.call_id
                )
                return self.outcome(state, turn, action)
            return await self.transfer(state, turn, "R-08")
        if result.alternatives:
            state = state.model_copy(
                update={"client_id": None, "phase": Phase.IDENTIFYING, "authorization": None}
            )
            return state, reply(
                state,
                turn.turn_id,
                "Дата рождения неоднозначна. Назовите ФИО и дату рождения с четырёхзначным годом.",
                code="BIRTH_DATE_AMBIGUOUS",
            )
        if state.client_id is None and slots.last_name and slots.first_name and slots.birth_date:
            try:
                patient = await self.booking.identify(
                    slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
                )
            except BookingError:
                state = state.model_copy(update={"identity_attempts": state.identity_attempts + 1})
                if state.identity_attempts >= 2:
                    return await self.transfer(state, turn, "R-07")
                return state, reply(
                    state,
                    turn.turn_id,
                    "Не удалось однозначно найти пациента. Уточните ФИО и дату рождения.",
                    code="IDENTIFICATION_REQUIRED",
                )
            state = state.model_copy(update={"client_id": patient.id, "misunderstandings": 0})
            return await self.identify_or_list(state, turn)
        if state.phase == Phase.CANCEL_CHOOSING:
            if "choice" in intents and slots.choice_index is not None:
                if not 1 <= slots.choice_index <= len(state.visit_items):
                    raise BookingError("INVALID_CHOICE")
                return await self.choose(state, turn, state.visit_items[slots.choice_index - 1])
            if state.visit_has_more and "дальше" in turn.text.casefold():
                return await self.identify_or_list(state, turn, next_page=True)
        if state.phase == Phase.CANCEL_OFFER:
            if "I-02" in intents or "перенес" in turn.text.casefold():
                return await self.transfer(state, turn, "R-08")
            if "I-03" in intents or "no" in intents:
                return await self.prepare(state, turn)
        if state.phase == Phase.CANCEL_CONFIRM:
            if "repeat" in intents:
                return self.confirmation(state, turn)
            refinements = {
                "specialty",
                "doctor",
                "date_from",
                "date_to",
                "time_from",
                "time_to",
                "visit_ref",
            } & slots.model_fields_set
            if refinements or "I-02" in intents:
                await self.cancellation.store.release_call(state.call_id)
                state = state.model_copy(
                    update={
                        "visit_query": query_from_slots(slots, state.visit_query),
                        "cancel_proposal": None,
                    }
                )
                return await self.identify_or_list(state, turn)
            if "yes" in intents:
                if not await self.store.authorize(
                    state.call_id, state.version, state.input_epoch, turn.turn_id
                ):
                    raise BookingError("CONFIRMATION_INTERRUPTED")
                action = await self.cancellation.confirm(
                    Approval(
                        call_id=state.call_id,
                        action_id=state.cancel_proposal.action_id,
                        revision=state.cancel_proposal.revision,
                        turn_id=turn.turn_id,
                        is_final=True,
                        permitted=True,
                        guard_version=state.version,
                        input_epoch=state.input_epoch,
                    )
                )
                return self.outcome(state, turn, action)
        if "no" in intents:
            await self.cancellation.store.release_call(state.call_id)
            state = await self.store.end(state.call_id)
            return state, reply(state, turn.turn_id, "Всего доброго!", code="CALL_ENDED")
        if state.phase == Phase.CANCELLED and "I-03" in intents:
            return await self.start(state, turn, result)
        if "repeat" in intents and state.client_id is not None:
            return await self.identify_or_list(state, turn)
        state = state.model_copy(
            update={"misunderstandings": state.misunderstandings + 1, "authorization": None}
        )
        if state.misunderstandings >= 3:
            return await self.transfer(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Уточните, пожалуйста, какую запись хотите отменить.",
            code="NOT_UNDERSTOOD",
        )
