"""Текстовый маршрутизатор: NLU Ильи, сценарии С-01/С-05 и правила действий backend."""

from datetime import datetime
from typing import Literal

from app.booking.models import ActionState, Approval, BookingError
from app.booking.preferences import PREFERENCE_FIELDS, SlotPreferences
from app.booking.service import BookingService
from app.cancellation.models import CancelState
from app.cancellation.service import CancellationService
from app.contracts import NluResult, SafetyDecision
from app.diagnostics.recorder import emit
from app.dialogue.cancellation import CancellationConversation
from app.dialogue.cross_scenario import CrossScenario
from app.dialogue.cross_scenario import classify as classify_cross_scenario
from app.dialogue.doctors import DoctorNames
from app.dialogue.failures import mis_failure
from app.dialogue.identity import IdentityDrafts
from app.dialogue.information import InformationConversation
from app.dialogue.medical import MedicalConversation
from app.dialogue.models import DialogueState, Phase, Proposal, Reply, TransferRequest, Turn, reply
from app.dialogue.nlu_adapter import RulesInput
from app.dialogue.ports import DialogueStore, NluInterpreter, SafetyChecker
from app.dialogue.presentation import BookingPresenter
from app.dialogue.rescheduling import RescheduleConversation
from app.dialogue.safety_adapter import route_for
from app.dialogue.transitions import handoff_reply
from app.dialogue.visit_privacy import mentions_existing_visits, requested_owner
from app.dialogue.visits import VisitConversation, wants_new_booking
from app.information.catalog import information_service
from app.mis.ports import MisError
from app.rescheduling.models import MoveState
from app.rescheduling.service import RescheduleService
from app.scheduling.matching import profile_key
from app.visits.models import VisitQuery
from app.visits.ports import DoctorRooms
from app.visits.service import VisitService

FOLLOW_UP_QUESTION = "Могу ещё чем-то помочь?"


class BookingConversation:
    def __init__(
        self,
        booking: BookingService,
        store: DialogueStore,
        safety: SafetyChecker,
        *,
        nlu: NluInterpreter | None = None,
        rooms: DoctorRooms | None = None,
        cancellation: CancellationService | None = None,
        rescheduling: RescheduleService | None = None,
        presenter: BookingPresenter | None = None,
        identity_drafts: IdentityDrafts | None = None,
    ) -> None:
        self.booking = booking
        self.store = store
        self.safety = safety
        self.identity_drafts = identity_drafts
        if cancellation is not None and cancellation.operations is None:
            cancellation.operations = booking.operations
        self.rooms = rooms
        self.presenter = presenter if presenter is not None else BookingPresenter(booking.clock)
        self.information = InformationConversation(
            information_service(), self.presenter, store, self.information_continuation
        )
        self.input = (
            nlu if nlu is not None else RulesInput(booking.mis, booking.clock, booking.schedule)
        )
        self.names = DoctorNames(booking.mis, booking.schedule)
        self.visits = VisitConversation(
            booking, store, VisitService(booking.mis, booking.clock, rooms), self.transfer
        )
        self.cancellation = (
            CancellationConversation(booking, cancellation, store, self.transfer)
            if cancellation is not None
            else None
        )
        self.rescheduling = (
            RescheduleConversation(rescheduling, store, self.transfer, rooms)
            if rescheduling
            else None
        )
        self.medical = MedicalConversation(
            booking,
            store,
            self.transfer,
            cancellation=cancellation,
            rescheduling=rescheduling,
            release=self.release_call,
        )

    async def release_call(self, call_id: str) -> None:
        await self.booking.store.release_call_holds(call_id)
        if self.cancellation is not None:
            await self.cancellation.cancellation.store.release_call(call_id)
        if self.rescheduling is not None:
            await self.rescheduling.service.store.release_call(call_id)

    async def short_reply(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply] | None:
        decision = self.safety.evaluate(state.call_id, turn)
        medical_followup = self.information.medical_followup(state, turn)
        if decision.flags & {"medical", "robot", "no_recording"} or medical_followup:
            state = state.model_copy(
                update={"information_topic": None, "information_clarifying": False}
            )
        if "medical" in decision.flags or medical_followup:
            state, result = await self.medical.question(state, turn)
            return state, self.information.laboratory_boundary(
                result, turn, contextual=medical_followup
            )
        if decision.flags & {"robot", "no_recording"}:
            return await self.service_question(state, turn, decision.flags)
        if state.medical_boundary_pending and not state.information_clarifying:
            answer = self.information.short_answer(turn.text)
            if answer == "no":
                return await self.medical.decline(state, turn)
            if answer == "repeat":
                return self.medical.prompt(state, turn)
        return await self.information.step(state, turn)

    async def service_question(
        self, state: DialogueState, turn: Turn, flags: frozenset[str]
    ) -> tuple[DialogueState, Reply]:
        """Служебный ответ не создаёт нового вопроса согласия и не продлевает бронь."""
        state = state.model_copy(update={"authorization": None})
        if self.rescheduling and state.move_proposal:
            action = await self.rescheduling.service.store.get(state.move_proposal.action_id)
            if action.state != MoveState.HELD:
                state = state.model_copy(update={"phase": Phase.MOVE_PENDING})
        phrases = []
        if "robot" in flags:
            phrases.append("Я виртуальный помощник регистратуры.")
        if "no_recording" in flags:
            state = state.model_copy(update={"recording_opt_out": True})
            phrases.append("Хорошо, запись разговора отключена.")
        continuation = (
            ("Чем могу помочь?",)
            if state.phase == Phase.READY and not state.medical_boundary_pending
            else self.information.resume_question(state, turn)
        )
        return state, reply(
            state,
            turn.turn_id,
            *phrases,
            *continuation,
            code="RECORDING_OPT_OUT" if "no_recording" in flags else "ROBOT_DISCLOSED",
        )

    async def reset_identity(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply | None]:
        """Новый поиск не наследует старого пациента даже при отказе/429 МИС."""
        pending = state.phase in {
            Phase.RECONCILING,
            Phase.CANCEL_RECONCILING,
            Phase.MOVE_PENDING,
            Phase.MOVE_FINISH_CONFIRM,
        }
        if state.proposal:
            action = await self.booking.store.get(state.proposal.action_id)
            pending |= action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }
        if self.cancellation and state.cancel_proposal:
            action = await self.cancellation.cancellation.store.get(state.cancel_proposal.action_id)
            pending |= action.state in {CancelState.COMMITTING, CancelState.RECONCILING}
        if self.rescheduling and state.move_proposal:
            action = await self.rescheduling.service.store.get(state.move_proposal.action_id)
            pending |= action.state in {
                MoveState.CREATING,
                MoveState.CREATE_UNKNOWN,
                MoveState.NEW_CREATED,
                MoveState.CANCELLING,
                MoveState.CANCEL_UNKNOWN,
                MoveState.MANUAL,
            }
        if pending:
            # Уже отправленное изменение остаётся в журнале T06. Новый пациент
            # не вправе продолжать его, а локальный сброс не отменяет запрос МИС.
            return await self.transfer(state, turn, "R-16")
        await self.release_call(state.call_id)
        self.booking.clear_call_cache(state.call_id)
        return state.model_copy(
            update={
                "client_id": None,
                "phase": Phase.IDENTIFYING,
                "proposal": None,
                "cancel_proposal": None,
                "move_proposal": None,
                "cancel_target": None,
                "move_target": None,
                "authorization": None,
                "options": (),
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "visit_query": VisitQuery(),
                "visit_id": None,
                "identity_draft": None,
                "identity_attempts": 0,
                "misunderstandings": 0,
                "retry_at": None,
                "mis_wait_since": None,
            }
        ), None

    def information_continuation(self, state: DialogueState, turn: Turn) -> tuple[str, ...]:
        """Повтор действующего вопроса только из локальных данных, без чтений МИС."""
        if state.phase == Phase.HELD and state.proposal is not None:
            room = (
                self.rooms.room(state.proposal.slot.doctor_id, state.proposal.slot.scheduled_at)
                if self.rooms
                else None
            )
            return self.presenter.confirmation(
                state.proposal.slot, state.proposal.doctor_name, room
            )
        if state.phase == Phase.CANCEL_CONFIRM and self.cancellation and state.cancel_proposal:
            return self.cancellation.confirmation(state, turn)[1].utterances
        if (
            state.phase in {Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}
            and self.rescheduling
            and state.move_proposal
        ):
            return self.rescheduling.confirmation(state, turn)[1].utterances
        if state.phase == Phase.CANCEL_OFFER and state.cancel_target:
            return (
                CancellationConversation.visit_phrase(state.cancel_target),
                "Могу перенести запись вместо отмены. Перенести или отменить?",
            )
        if state.phase == Phase.MOVE_SOURCE and state.move_target:
            return (
                CancellationConversation.visit_phrase(state.move_target),
                "Эту запись переносим? Укажите новый день или время.",
            )
        return ()

    async def transfer(
        self,
        state: DialogueState,
        turn: Turn,
        route: str,
        priority: Literal["P1", "P2", "P3"] = "P3",
        *,
        reason: Literal["move_create_rejected", "move_manual", "mis_wait"] | None = None,
    ) -> tuple[DialogueState, Reply]:
        state = await self.store.preempt(
            state.call_id,
            turn.turn_id,
            TransferRequest(route=route, priority=priority, reason=reason),
        )
        await self.release_call(state.call_id)
        return state, handoff_reply(state, turn.turn_id)

    async def offer(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        await self.booking.store.release_call_holds(state.call_id)
        state = state.model_copy(
            update={
                "proposal": None,
                "authorization": None,
                "options": (),
                "phase": Phase.IDENTIFIED,
            }
        )
        try:
            options = await self.booking.options(
                state.client_id, state.specialty, state.preferences, limit=2, call_id=state.call_id
            )
            doctors = {d.id: d for d in await self.names.speaking()}
        except MisError as error:
            # Отменённая старая бронь и новые предпочтения должны сохраниться даже при ожидании.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError as error:
            if str(error) == "ELIGIBILITY_UNKNOWN":
                return state, reply(
                    state,
                    turn.turn_id,
                    "В расписании есть время, но я не могу подтвердить условия записи к этому специалисту: прикрепление, направление или другой необходимый допуск.",
                    "Это нужно уточнить у регистратора. Можно выбрать другого специалиста или попросить оператора.",
                    code="ELIGIBILITY_UNKNOWN",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Подбор недоступен. Уточните данные или попросите регистратора.",
                code="BOOKING_REJECTED",
            )
        state = state.model_copy(
            update={"options": options, "phase": Phase.OFFERED if options else Phase.IDENTIFIED}
        )
        if not options:
            return state, reply(
                state,
                turn.turn_id,
                "Подходящих подтверждённых слотов нет. Уточните предпочтения или попросите регистратора.",
                code="NO_SLOTS",
            )
        offered, phrases = self.presenter.offer(options, doctors)
        state = state.model_copy(
            update={
                "options": offered,
                "shown_option_keys": tuple(self._slot_key(slot) for slot in offered),
            }
        )
        return state, reply(state, turn.turn_id, *phrases)

    @staticmethod
    def _slot_key(slot) -> str:
        return f"{slot.doctor_id}:{slot.scheduled_at.isoformat()}"

    async def cross_scenario_reply(
        self, state: DialogueState, turn: Turn, result: NluResult | None = None
    ) -> tuple[DialogueState, Reply] | None:
        kind = classify_cross_scenario(turn.text)
        if kind is None:
            return None
        if kind == CrossScenario.WAIT:
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо, подожду. Скажите «продолжим», когда будете готовы.",
                code="WAIT_REQUESTED",
            )
        if kind == CrossScenario.GOODBYE:
            if state.phase in {
                Phase.HELD,
                Phase.CANCEL_CONFIRM,
                Phase.MOVE_CONFIRM,
                Phase.MOVE_FINISH_CONFIRM,
            }:
                state = state.model_copy(update={"end_confirmation_pending": True})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Перед завершением уточню: закончить разговор без выполнения этого действия?",
                    code="END_CONFIRMATION_REQUIRED",
                )
            await self.release_call(state.call_id)
            ended = await self.store.end(state.call_id)
            return ended, reply(
                ended, turn.turn_id, "Спасибо за обращение. До свидания.", code="CALL_ENDED"
            )
        if kind == CrossScenario.RESET:
            if state.phase in {
                Phase.RECONCILING,
                Phase.CANCEL_RECONCILING,
                Phase.MOVE_PENDING,
                Phase.MOVE_FINISH_CONFIRM,
            }:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Действие ещё проверяется. Повторно отправлять его не буду.",
                    "Скажите «проверьте результат» или попросите регистратора.",
                    code="ACTION_PENDING",
                )
            if state.reminder is not None:
                state = state.model_copy(update={"end_confirmation_pending": False})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Хорошо, начнём сначала: вы придёте на приём? Ответьте «да» или «нет».",
                    code="REMINDER_RESTART",
                )
            await self.release_call(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.READY,
                    "client_id": None,
                    "options": (),
                    "shown_option_keys": (),
                    "proposal": None,
                    "cancel_proposal": None,
                    "cancel_target": None,
                    "move_proposal": None,
                    "move_target": None,
                    "authorization": None,
                    "specialty": None,
                    "preferences": type(state.preferences)(),
                    "purpose": "booking",
                    "visit_id": None,
                    "identity_draft": None,
                    "identity_attempts": 0,
                    "visit_items": (),
                    "visit_cursor": None,
                    "visit_has_more": False,
                    "visit_query": VisitQuery(),
                    "query_for_self": False,
                    "cancel_reschedule_offered": False,
                    "information_topic": None,
                    "information_clarifying": False,
                    "medical_boundary_pending": False,
                    "misunderstandings": 0,
                    "end_confirmation_pending": False,
                    "retry_at": None,
                    "mis_wait_since": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо, начнём сначала. Чем могу помочь?",
                code="DIALOGUE_RESTARTED",
            )
        if kind == CrossScenario.MORE_OPTIONS:
            if state.phase == Phase.OFFERED and state.client_id is not None and state.specialty:
                excluded = frozenset(
                    (int(key.split(":", 1)[0]), datetime.fromisoformat(key.split(":", 1)[1]))
                    for key in state.shown_option_keys
                )
                try:
                    options = await self.booking.options(
                        state.client_id,
                        state.specialty,
                        state.preferences,
                        limit=2,
                        exclude=excluded,
                        call_id=state.call_id,
                    )
                except MisError as error:
                    return mis_failure(state, turn, self.booking.clock.now(), error)
                if options:
                    doctors = {d.id: d for d in await self.names.speaking()}
                    offered, phrases = self.presenter.offer(options, doctors)
                    state = state.model_copy(
                        update={
                            "options": offered,
                            "shown_option_keys": state.shown_option_keys
                            + tuple(self._slot_key(slot) for slot in offered),
                        }
                    )
                    return state, reply(state, turn.turn_id, *phrases, code="MORE_OPTIONS")
                return state, reply(
                    state,
                    turn.turn_id,
                    "Других подтверждённых вариантов пока нет. Назовите другой день или время.",
                    code="NO_MORE_OPTIONS",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Сначала уточним текущий вопрос. Если хотите, назовите другой день или время.",
                code="MORE_OPTIONS_CONTEXT",
            )
        if kind == CrossScenario.UNSURE:
            if state.phase in {Phase.HELD, Phase.CANCEL_CONFIRM, Phase.MOVE_CONFIRM}:
                if state.misunderstandings >= 1:
                    return await self.transfer(state, turn, "R-05")
                state = state.model_copy(update={"misunderstandings": 1, "authorization": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Не расслышала точный ответ. Подтверждаете действие? Ответьте «да» или «нет».",
                    code="CONFIRMATION_CLARIFICATION",
                )
            if state.reminder is not None:
                if state.misunderstandings >= 1:
                    return await self.transfer(state, turn, "R-05")
                state = state.model_copy(update={"misunderstandings": 1})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Вы придёте на приём? Ответьте, пожалуйста, точно: «да» или «нет».",
                    code="REMINDER_CLARIFICATION",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Нужен точный ответ: да или нет?",
                code="YES_NO_CLARIFICATION",
            )
        if kind == CrossScenario.FILLER:
            question = self.information.resume_question(state, turn)
            return state, reply(
                state,
                turn.turn_id,
                "Не расслышала ответ.",
                *(question or ("Повторите, пожалуйста, ответ на текущий вопрос.",)),
                code="CONTEXT_REPROMPT",
            )
        if kind == CrossScenario.THIRD_PARTY:
            return await self.visits.privacy_transfer(state, turn)
        if kind == CrossScenario.SWITCH_CANCEL:
            if result is None and state.reminder is not None:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Сейчас идёт звонок-напоминание. Изменить запись в нём нельзя.",
                    "Для отмены обратитесь в регистратуру.",
                    code="REMINDER_SCOPE",
                )
            if result is None or self.cancellation is None:
                return None
            return await self.cancellation.start(state, turn, result)
        if kind == CrossScenario.SWITCH_VISITS:
            if result is None:
                if state.reminder is not None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Сейчас идёт звонок-напоминание. Сведения о других записях в нём не проверяю.",
                        "Обратитесь в регистратуру, если хотите проверить свои записи.",
                        code="REMINDER_SCOPE",
                    )
                return None
            return await self.visits.start(state, turn, result)
        return None

    async def step(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        # Новая предметная просьба завершает справочное уточнение; её данные не теряются.
        state = state.model_copy(
            update={"information_topic": None, "information_clarifying": False}
        )
        result = await self.input.parse(state, turn)
        if result.call_id != state.call_id or result.turn_id != turn.turn_id:
            raise RuntimeError("результат NLU не относится к текущей реплике")
        emit("nlu.parsed", **result.model_dump(mode="json", exclude={"call_id", "turn_id"}))
        decision = self.safety.evaluate(state.call_id, turn, result.flags)
        transfer = route_for(decision)
        if transfer:
            return await self.transfer(state, turn, transfer.route, transfer.priority)
        if "medical" in decision.flags or "I-13" in result.intents:
            return await self.medical.question(state, turn)
        if decision.flags & {"robot", "no_recording"}:
            return await self.service_question(state, turn, decision.flags)
        cross_reply = await self.cross_scenario_reply(state, turn, result)
        if cross_reply is not None:
            return cross_reply
        if state.client_id is not None and (
            any(
                getattr(result.slots, field) is not None
                for field in ("last_name", "first_name", "middle_name", "birth_date")
            )
            or result.alternatives
        ):
            state, stopped = await self.reset_identity(state, turn)
            if stopped is not None:
                return state, stopped
        try:
            return await self.advance(state, turn, result, decision)
        except MisError as error:
            # Внешний _process видит состояние до входа в step. Ошибка нового
            # поиска должна сохранить очищенную личность, а не старую бронь.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError as error:
            if str(error) != "IDENTIFICATION_REQUIRED":
                raise
            return state, reply(
                state,
                turn.turn_id,
                "Не удалось однозначно найти пациента. Назовите ФИО и дату рождения ещё раз.",
                code="IDENTIFICATION_REQUIRED",
            )

    async def advance(
        self, state: DialogueState, turn: Turn, result: NluResult, decision: SafetyDecision
    ) -> tuple[DialogueState, Reply]:
        slots, intents = result.slots, set(result.intents)
        if state.medical_boundary_pending:
            refinements = PREFERENCE_FIELDS & slots.model_fields_set
            if "repeat" in intents:
                return self.medical.prompt(state, turn)
            if "no" in intents and not ("I-01" in intents or slots.specialty):
                return await self.medical.decline(state, turn)
            # Уточнение дня начинает подбор, но не служит подтверждением записи.
            if "yes" in intents or (refinements and not intents & {"no", "maybe", "opt_out"}):
                if state.phase in {
                    Phase.RECONCILING,
                    Phase.CANCEL_RECONCILING,
                    Phase.MOVE_PENDING,
                    Phase.MOVE_FINISH_CONFIRM,
                }:
                    return await self.transfer(state, turn, "R-08")
                state = self.visits.booking_state(state).model_copy(
                    update={"medical_boundary_pending": False, "specialty": None}
                )
                slots = slots.model_copy(update={"specialty": slots.specialty or "терапевт"})
                intents.add("I-01")
            elif intents & {"I-01", "I-02", "I-04"} or slots.specialty or slots.doctor:
                state = state.model_copy(update={"medical_boundary_pending": False})
            elif refinements:
                return self.medical.prompt(state, turn)
        if slots.on_behalf in {"other", "child"} or (
            requested_owner(turn.text) == "other"
            and (
                state.purpose in {"visits", "cancel", "reschedule"}
                or intents & {"I-02", "I-03", "I-04"}
                or mentions_existing_visits(turn.text)
            )
        ):
            return await self.visits.privacy_transfer(state, turn)
        # Сохраняем только зашифрованное ФИО. Общий шаг работает для записи, справки,
        # отмены и переноса; окончательная проверка пациента остаётся строгой.
        if state.phase == Phase.IDENTIFYING and self.identity_drafts is not None:
            has_name = bool(slots.last_name and slots.first_name)
            if (slots.last_name or slots.first_name or slots.middle_name) and not has_name:
                state = state.model_copy(update={"identity_draft": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите фамилию, имя и отчество полностью.",
                    code="IDENTIFICATION_REQUIRED",
                )
            if has_name:
                state = state.model_copy(
                    update={
                        "identity_draft": self.identity_drafts.seal(
                            state.call_id, slots.last_name, slots.first_name, slots.middle_name
                        )
                    }
                )
            if slots.birth_date and not result.alternatives and state.identity_draft:
                names = self.identity_drafts.open(state.call_id, state.identity_draft)
                state = state.model_copy(update={"identity_draft": None})
                if names is None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Назовите фамилию, имя, отчество и дату рождения ещё раз.",
                        code="IDENTIFICATION_REQUIRED",
                    )
                slots = slots.model_copy(update=names)
                result = result.model_copy(update={"slots": slots})
            elif has_name and not slots.birth_date:
                state = state.model_copy(update={"misunderstandings": 0})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Спасибо. Назовите дату рождения: день, месяц и год.",
                    code="BIRTH_DATE_REQUIRED",
                )
        if self.rescheduling and (state.purpose == "reschedule" or "I-02" in intents):
            sent = False
            if state.move_proposal:
                known = await self.rescheduling.service.store.get(state.move_proposal.action_id)
                sent = known.state not in {MoveState.HELD, MoveState.ABORTED, MoveState.EXPIRED}
            if state.phase == Phase.MOVE_EMPTY and (
                "yes" in intents or "I-01" in intents or slots.specialty
            ):
                state = self.visits.booking_state(state)
                if slots.specialty is None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "К какому врачу хотите записаться?",
                        code="SPECIALTY_REQUIRED",
                    )
                intents.add("I-01")
            elif state.purpose == "reschedule" and (
                "I-03" in intents or "I-01" in intents and wants_new_booking(turn.text)
            ):
                if sent:
                    return await self.transfer(state, turn, "R-08")
                await self.rescheduling.service.store.release_call(state.call_id)
                state = self.visits.booking_state(state).model_copy(
                    update={"move_proposal": None, "move_target": None}
                )
            elif (
                state.purpose == "reschedule"
                and "I-04" in intents
                and state.phase == Phase.RESCHEDULED
            ):
                return await self.visits.start(state, turn, result)
            else:
                try:
                    if (
                        state.purpose != "reschedule"
                        or state.move_target is None
                        and state.move_proposal is None
                        and state.phase
                        not in {Phase.IDENTIFYING, Phase.MOVE_CHOOSING, Phase.MOVE_EMPTY}
                    ):
                        await self.release_call(state.call_id)
                        return await self.rescheduling.start(state, turn, result)
                    return await self.rescheduling.step(state, turn, result)
                except BookingError as error:
                    if str(error) in {"ACTION_PENDING", "CANCELLATION_BUSY"}:
                        return await self.transfer(state, turn, "R-08")
                    if state.move_proposal:
                        action = await self.rescheduling.service.store.get(
                            state.move_proposal.action_id
                        )
                        if action.state not in {
                            MoveState.HELD,
                            MoveState.ABORTED,
                            MoveState.EXPIRED,
                        }:
                            if str(error) == "CONFIRMATION_INTERRUPTED":
                                raise
                            return await self.transfer(state, turn, "R-06")
                    if str(error) in {"VISIT_CHANGED", "VISIT_PAST", "HOLD_NOT_ACTIVE"}:
                        await self.rescheduling.service.store.release_call(state.call_id)
                        state = state.model_copy(
                            update={
                                "move_proposal": None,
                                "move_target": None,
                                "options": (),
                                "phase": Phase.MOVE_CHOOSING,
                            }
                        )
                        return state, reply(
                            state,
                            turn.turn_id,
                            "Прежняя запись изменилась. Уточните запись для переноса.",
                            code="VISIT_CHANGED",
                        )
                    raise
        if self.cancellation is not None and (state.purpose == "cancel" or "I-03" in intents):
            if (
                state.purpose == "cancel"
                and "I-04" in intents
                and "I-03" not in intents
                and state.phase != Phase.CANCEL_RECONCILING
            ):
                await self.cancellation.cancellation.store.release_call(state.call_id)
                state = state.model_copy(update={"cancel_proposal": None, "cancel_target": None})
                return await self.visits.start(state, turn, result)
            if (
                state.purpose == "cancel"
                and "I-01" in intents
                and state.phase != Phase.CANCEL_RECONCILING
            ):
                await self.cancellation.cancellation.store.release_call(state.call_id)
                state = self.visits.booking_state(state).model_copy(
                    update={"cancel_proposal": None, "cancel_target": None}
                )
            else:
                try:
                    if state.purpose != "cancel":
                        return await self.cancellation.start(state, turn, result)
                    return await self.cancellation.step(state, turn, result)
                except BookingError as error:
                    if str(error) in {"ACTION_PENDING", "CANCELLATION_BUSY"}:
                        return await self.transfer(state, turn, "R-08")
                    if str(error) in {"VISIT_CHANGED", "VISIT_PAST", "HOLD_NOT_ACTIVE"}:
                        await self.cancellation.cancellation.store.release_call(state.call_id)
                        state = state.model_copy(
                            update={
                                "phase": Phase.CANCEL_CHOOSING,
                                "cancel_proposal": None,
                                "cancel_target": None,
                                "visit_items": (),
                                "authorization": None,
                            }
                        )
                        return state, reply(
                            state,
                            turn.turn_id,
                            "Данные записи изменились или подтверждение истекло. Скажите «повторите», чтобы получить актуальные записи.",
                            code="VISIT_CHANGED",
                        )
                    raise
        if state.purpose == "visits":
            if "I-01" in intents and (
                state.phase != Phase.VISITS_SHOWN
                or not state.visit_items
                or wants_new_booking(turn.text)
            ):
                state = self.visits.booking_state(state)
            elif intents & {"I-02", "I-03"}:
                return await self.transfer(state, turn, "R-08")
            else:
                return await self.visits.step(state, turn, result)
        if "I-04" in intents:
            if state.proposal is not None:
                known = await self.booking.store.get(state.proposal.action_id)
                if known.state == ActionState.HELD:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Запись пока не создана. Подтвердите выбранный приём.",
                        code="CONFIRMATION_REQUIRED",
                    )
                action = await self.booking.reconcile(state.proposal.action_id)
                if action.state == ActionState.BOOKED:
                    confirmed = self.booked_phrase(action, state.proposal.doctor_name)
                    state = state.model_copy(
                        update={
                            "phase": Phase.BOOKED,
                            "visit_id": action.visit_id,
                            "proposal": None,
                            "options": (),
                        }
                    )
                    return state, reply(
                        state, turn.turn_id, confirmed, FOLLOW_UP_QUESTION, code="BOOKED"
                    )
                state = state.model_copy(update={"phase": Phase.RECONCILING})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Результат всё ещё уточняется. Повторную запись не отправляю.",
                    code="RECONCILING",
                )
            return await self.visits.start(state, turn, result)
        if state.phase == Phase.RECONCILING:
            if "no" in intents or intents & {"I-01", "I-02", "I-03"}:
                # Уже отправленная операция не отменяется местным снятием брони.
                return await self.transfer(state, turn, "R-08")
            return state, reply(
                state,
                turn.turn_id,
                "Результат ещё уточняется. Для сверки спросите: на когда я записана?",
                code="RECONCILING",
            )
        if state.phase == Phase.BOOKED and not slots.model_fields_set:
            if intents == {"yes"}:
                state = self.visits.booking_state(state)
                return state, reply(
                    state, turn.turn_id, "Чем ещё могу помочь?", code="MORE_HELP_REQUIRED"
                )
            if intents == {"no"}:
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            if intents == {"repeat"}:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Запись уже подтверждена.",
                    FOLLOW_UP_QUESTION,
                    code="MORE_HELP_OFFERED",
                )
        if intents & {
            "I-02",
            "I-03",
            "I-05",
            "I-06",
            "I-07",
            "I-08",
            "I-09",
            "I-10",
            "I-12",
            "I-13",
            "I-14",
        }:
            return await self.transfer(state, turn, "R-08")
        if slots.on_behalf in {"other", "child"}:
            return await self.transfer(state, turn, "R-16")
        if slots.policy_number is not None:
            return await self.transfer(state, turn, "R-07")
        if result.alternatives:
            await self.booking.store.release_call_holds(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.IDENTIFYING,
                    "client_id": None,
                    "proposal": None,
                    "options": (),
                    "authorization": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Дата рождения неоднозначна. Назовите ФИО и дату рождения с четырёхзначным годом.",
                code="BIRTH_DATE_AMBIGUOUS",
            )
        if slots.last_name and slots.first_name and slots.birth_date:
            patient = await self.booking.identify(
                slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
            )
            state = state.model_copy(
                update={"client_id": patient.id, "phase": Phase.IDENTIFIED, "misunderstandings": 0}
            )
            if state.specialty:
                return await self.offer(state, turn)
            return state, reply(
                state, turn.turn_id, "Пациент найден. К какому специалисту хотите записаться?"
            )
        preference_fields = PREFERENCE_FIELDS & slots.model_fields_set
        if (
            slots.specialty is not None
            or slots.doctor is not None
            or preference_fields
            or "I-01" in intents
        ):
            changes = {field: getattr(slots, field) for field in preference_fields}
            if slots.doctor is not None:
                doctors = [
                    d
                    for d in await self.names.speaking()
                    if d.full_name.split()[0].casefold() == slots.doctor.casefold()
                ]
                if not doctors:
                    raise BookingError("DOCTOR_UNAVAILABLE")
                changes["doctor_ids"] = tuple(d.id for d in doctors)
                specialties = {d.specialty for d in doctors}
                if len(specialties) != 1:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Уточните специальность врача.",
                        code="DOCTOR_AMBIGUOUS",
                    )
                if slots.specialty is None:
                    state = state.model_copy(update={"specialty": next(iter(specialties))})
            if slots.specialty is not None:
                if state.specialty is None or profile_key(state.specialty) != profile_key(
                    slots.specialty
                ):
                    state = state.model_copy(update={"preferences": SlotPreferences()})
                state = state.model_copy(update={"specialty": slots.specialty})
            state = state.model_copy(
                update={
                    "preferences": state.preferences.model_copy(update=changes),
                    "misunderstandings": 0,
                }
            )
            if state.client_id is None:
                state = state.model_copy(update={"phase": Phase.IDENTIFYING})
                return state, reply(
                    state,
                    turn.turn_id,
                    self.presenter.ask_patient(),
                )
            if state.specialty:
                return await self.offer(state, turn)
        if "choice" in intents and state.phase == Phase.OFFERED:
            index = slots.choice_index
            if index is None or not 1 <= index <= len(state.options):
                raise BookingError("INVALID_CHOICE")
            doctors = {d.id: d for d in await self.names.speaking()}
            action = await self.booking.propose(
                state.call_id, state.client_id, state.options[index - 1]
            )
            state = state.model_copy(
                update={
                    "phase": Phase.HELD,
                    "proposal": Proposal(
                        action_id=action.action_id,
                        revision=action.revision,
                        slot=action.slot,
                        expires_at=action.expires_at,
                        doctor_name=doctors[action.slot.doctor_id].full_name,
                    ),
                    "misunderstandings": 0,
                }
            )
            return state, await self.confirmation_phrase(state, turn)
        if "yes" in intents and state.phase == Phase.HELD and state.proposal is not None:
            if not decision.final or not await self.store.authorize(
                state.call_id, state.version, state.input_epoch, turn.turn_id
            ):
                raise BookingError("CONFIRMATION_INTERRUPTED")
            action = await self.booking.confirm(
                Approval(
                    call_id=state.call_id,
                    action_id=state.proposal.action_id,
                    revision=state.proposal.revision,
                    turn_id=turn.turn_id,
                    is_final=True,
                    permitted=True,
                    guard_version=state.version,
                    input_epoch=state.input_epoch,
                )
            )
            state = state.model_copy(update={"authorization": None})
            if action.state == ActionState.BOOKED:
                confirmed = self.booked_phrase(action, state.proposal.doctor_name)
                state = state.model_copy(
                    update={
                        "phase": Phase.BOOKED,
                        "visit_id": action.visit_id,
                        "options": (),
                        "proposal": None,
                    }
                )
                return state, reply(
                    state,
                    turn.turn_id,
                    confirmed,
                    FOLLOW_UP_QUESTION,
                    code="BOOKED",
                )
            if action.state in {ActionState.COMMITTING, ActionState.RECONCILING}:
                state = state.model_copy(update={"phase": Phase.RECONCILING})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Результат уточняется. Повторную запись не отправляю.",
                    code="RECONCILING",
                )
            state = state.model_copy(
                update={"phase": Phase.IDENTIFIED, "proposal": None, "options": ()}
            )
            return state, reply(
                state,
                turn.turn_id,
                "МИС отклонила запись. Запросите новые варианты.",
                code="BOOKING_REJECTED",
            )
        if "no" in intents:
            if state.phase == Phase.BOOKED:
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            await self.booking.store.release_call_holds(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.IDENTIFIED if state.client_id else Phase.READY,
                    "proposal": None,
                    "options": (),
                    "authorization": None,
                }
            )
            return state, reply(state, turn.turn_id, "Бронь снята. Уточните удобное время.")
        if "repeat" in intents:
            if state.phase == Phase.HELD:
                return state, await self.confirmation_phrase(state, turn)
            if state.client_id and state.specialty:
                return await self.offer(state, turn)
        count = state.misunderstandings + 1
        state = state.model_copy(update={"misunderstandings": count, "authorization": None})
        if count >= 3:
            return await self.transfer(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Не поняла. Повторите, пожалуйста."
            if count == 1
            else "Не удалось понять. Могу подготовить запрос регистратору.",
            code="NOT_UNDERSTOOD",
        )

    def room_phrase(self, slot) -> str:
        room = self.rooms.room(slot.doctor_id, slot.scheduled_at) if self.rooms else None
        return f", кабинет {room}" if room else ""

    def booked_phrase(self, action, doctor_name: str | None) -> str:
        room = (
            self.rooms.room(action.slot.doctor_id, action.slot.scheduled_at) if self.rooms else None
        )
        return self.presenter.completed(action.slot, doctor_name, room)

    async def confirmation_phrase(self, state: DialogueState, turn: Turn) -> Reply:
        slot = state.proposal.slot
        doctor_name = state.proposal.doctor_name
        if doctor_name is None:
            doctors = {d.id: d for d in await self.names.speaking()}
            doctor_name = doctors[slot.doctor_id].full_name
        return reply(
            state,
            turn.turn_id,
            *self.presenter.confirmation(
                slot,
                doctor_name,
                self.rooms.room(slot.doctor_id, slot.scheduled_at) if self.rooms else None,
            ),
            code="CONFIRMATION_REQUIRED",
        )
