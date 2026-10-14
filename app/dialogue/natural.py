"""Текстовый маршрутизатор: NLU Ильи, сценарии С-01/С-05 и правила действий backend."""

from typing import Literal

from app.booking.models import ActionState, Approval, BookingError
from app.booking.preferences import SlotPreferences
from app.booking.service import BookingService
from app.cancellation.service import CancellationService
from app.dialogue.cancellation import CancellationConversation
from app.dialogue.failures import mis_failure
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
from app.mis.ports import MisError
from app.rescheduling.models import MoveState
from app.rescheduling.service import RescheduleService
from app.scheduling.matching import profile_key
from app.visits.ports import DoctorRooms
from app.visits.service import VisitService


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
    ) -> None:
        self.booking = booking
        self.store = store
        self.safety = safety
        self.rooms = rooms
        self.presenter = presenter if presenter is not None else BookingPresenter(booking.clock)
        self.input = nlu if nlu is not None else RulesInput(booking.mis, booking.clock)
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
        if (
            self.rescheduling
            and state.move_proposal
            and set(decision.flags) & {"robot", "no_recording"}
        ):
            action = await self.rescheduling.service.store.get(state.move_proposal.action_id)
            if action.state == MoveState.HELD:
                await self.rescheduling.service.store.release_call(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.MOVE_OFFERED,
                        "move_proposal": None,
                        "authorization": None,
                    }
                )
            else:
                state = state.model_copy(
                    update={"phase": Phase.MOVE_PENDING, "authorization": None}
                )
        if "medical" in decision.flags:
            return await self.medical.question(state, turn)
        if "robot" in decision.flags:
            return state, reply(
                state,
                turn.turn_id,
                "Я виртуальный помощник регистратуры. Могу помочь с записью или подготовить запрос сотруднику.",
                code="ROBOT_DISCLOSED",
            )
        if "no_recording" in decision.flags:
            state = state.model_copy(update={"recording_opt_out": True, "authorization": None})
            return state, reply(
                state,
                turn.turn_id,
                "Пожелание не записывать разговор сохранено. Продолжим запись к врачу?",
                code="RECORDING_OPT_OUT",
            )
        return None

    async def transfer(
        self,
        state: DialogueState,
        turn: Turn,
        route: str,
        priority: Literal["P1", "P2", "P3"] = "P3",
        *,
        reason: Literal["move_create_rejected", "move_manual"] | None = None,
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
            doctors = {d.id: d for d in await self.booking.mis.doctors()}
        except MisError as error:
            # Отменённая старая бронь и новые предпочтения должны сохраниться даже при ожидании.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError:
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
        state = state.model_copy(update={"options": offered})
        return state, reply(state, turn.turn_id, *phrases)

    async def step(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        result = await self.input.parse(state, turn)
        if result.call_id != state.call_id or result.turn_id != turn.turn_id:
            raise RuntimeError("результат NLU не относится к текущей реплике")
        decision = self.safety.evaluate(state.call_id, turn, result.flags)
        transfer = route_for(decision)
        if transfer:
            return await self.transfer(state, turn, transfer.route, transfer.priority)
        if "medical" in decision.flags or "I-13" in result.intents:
            return await self.medical.question(state, turn)
        if "robot" in decision.flags:
            return state, reply(
                state,
                turn.turn_id,
                "Я виртуальный помощник регистратуры. Могу помочь с записью или подготовить запрос сотруднику.",
                code="ROBOT_DISCLOSED",
            )
        if "no_recording" in decision.flags:
            state = state.model_copy(update={"recording_opt_out": True, "authorization": None})
            return state, reply(
                state,
                turn.turn_id,
                "Пожелание не записывать разговор сохранено. Продолжим запись к врачу?",
                code="RECORDING_OPT_OUT",
            )
        slots, intents = result.slots, set(result.intents)
        if state.medical_boundary_pending:
            refinements = {
                "date_from",
                "date_to",
                "time_from",
                "time_to",
                "time_pref",
                "exclude_weekdays",
            } & slots.model_fields_set
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
                    return state, reply(state, turn.turn_id, confirmed, code="BOOKED")
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
        preference_fields = {
            "date_from",
            "date_to",
            "time_from",
            "time_to",
            "time_pref",
            "exclude_weekdays",
        } & slots.model_fields_set
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
                    for d in await self.booking.mis.doctors()
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
            doctors = {d.id: d for d in await self.booking.mis.doctors()}
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
            doctors = {d.id: d for d in await self.booking.mis.doctors()}
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
