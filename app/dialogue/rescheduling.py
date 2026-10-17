"""С-03: выбор прежней записи, новые предпочтения, согласие и безопасное завершение."""

import re

from app.booking.models import Approval, BookingError
from app.booking.preferences import PREFERENCE_FIELDS, SlotPreferences
from app.contracts import NluSlots
from app.dialogue.cancellation import CancellationConversation
from app.dialogue.doctors import DoctorNames
from app.dialogue.failures import mis_failure
from app.dialogue.models import Phase, reply
from app.dialogue.visit_text import room_text
from app.dialogue.visits import query_from_slots
from app.mis.ports import MisError
from app.rescheduling.models import MoveState, proposal
from app.visits.service import VisitService


class RescheduleConversation:
    def __init__(self, service, store, transfer, rooms=None):
        self.service, self.store, self.transfer, self.rooms = service, store, transfer, rooms
        self.visits = VisitService(service.mis, service.clock, rooms)
        self.doctors = DoctorNames(service.mis, service.booking.schedule)

    async def names(self, specialty: str):
        # Как фильтр specialty у стенда: без учёта регистра.
        wanted = specialty.casefold()
        return [d for d in await self.doctors.speaking() if d.specialty.casefold() == wanted]

    async def start(self, state, turn, result):
        if state.phase in {
            Phase.RECONCILING,
            Phase.CANCEL_RECONCILING,
            Phase.MOVE_PENDING,
            Phase.MOVE_FINISH_CONFIRM,
        }:
            return await self.transfer(state, turn, "R-08")
        selected = state.cancel_target if state.purpose == "cancel" else None
        if state.purpose == "visits" and len(state.visit_items) == 1 and not state.visit_has_more:
            selected = state.visit_items[0]
        slots = result.slots
        old_date = re.search(r"\bзаписан[аы]?\b", turn.text.casefold()) is not None
        source_slots = (
            slots
            if old_date
            else NluSlots(
                **{
                    k: getattr(slots, k)
                    for k in ("specialty", "visit_ref")
                    if k in slots.model_fields_set
                }
            )
        )
        preferences = SlotPreferences() if old_date else self.preferences(SlotPreferences(), slots)
        state = state.model_copy(
            update={
                "purpose": "reschedule",
                "phase": Phase.IDENTIFYING,
                "move_target": None,
                "move_proposal": None,
                "cancel_target": None,
                "cancel_proposal": None,
                "proposal": None,
                "options": (),
                "authorization": None,
                "visit_items": (),
                "visit_query": query_from_slots(source_slots),
                "preferences": preferences,
                "identity_attempts": 0,
                "misunderstandings": 0,
                "visit_id": None,
                "specialty": slots.specialty,
            }
        )
        if selected is not None:
            return self.choose(state, turn, selected)
        return await self.identify_or_list(state, turn)

    @staticmethod
    def preferences(previous, slots):
        return previous.model_copy(
            update={k: getattr(slots, k) for k in PREFERENCE_FIELDS & slots.model_fields_set}
        )

    async def identify_or_list(self, state, turn):
        if state.client_id is None:
            state = state.model_copy(update={"phase": Phase.IDENTIFYING})
            return state, reply(
                state,
                turn.turn_id,
                "Назовите фамилию, имя, отчество и дату рождения.",
                code="IDENTIFICATION_REQUIRED",
            )
        page = await self.visits.page(state.client_id, state.visit_query)
        state = state.model_copy(
            update={
                "phase": Phase.MOVE_CHOOSING,
                "visit_items": page.items,
                "visit_has_more": page.has_more,
                "move_target": None,
                "move_proposal": None,
                "authorization": None,
            }
        )
        if not page.items:
            state = state.model_copy(update={"phase": Phase.MOVE_EMPTY})
            return state, reply(
                state,
                turn.turn_id,
                "Будущих записей для переноса нет. Хотите записаться?",
                code="NO_ACTIVE_VISITS",
            )
        if len(page.items) == 1 and not page.has_more:
            return self.choose(state, turn, page.items[0])
        phrases = tuple(
            f"{i}. {CancellationConversation.visit_phrase(v)}" for i, v in enumerate(page.items, 1)
        )
        return state, reply(
            state,
            turn.turn_id,
            *phrases,
            "Записей больше трёх. Уточните специальность."
            if page.has_more
            else "Какую запись переносим?",
            code="MOVE_CHOICE_REQUIRED",
        )

    @staticmethod
    def choose(state, turn, selected):
        state = state.model_copy(
            update={
                "phase": Phase.MOVE_SOURCE,
                "move_target": selected,
                "specialty": selected.specialty,
                "visit_items": (),
                "visit_has_more": False,
                "options": (),
                "misunderstandings": 0,
            }
        )
        return state, reply(
            state,
            turn.turn_id,
            CancellationConversation.visit_phrase(selected),
            "Эту запись переносим? Укажите новый день или время.",
            code="MOVE_SOURCE_CONFIRMATION",
        )

    async def offer(self, state, turn, slots):
        if slots.specialty is not None and slots.specialty != state.move_target.specialty:
            raise BookingError("WRONG_SPECIALTY")
        preferences = self.preferences(state.preferences, slots)
        if slots.doctor is not None:
            doctors = [
                d
                for d in await self.names(state.move_target.specialty)
                if d.full_name.split()[0].casefold().replace("ё", "е")
                == slots.doctor.casefold().replace("ё", "е")
            ]
            if not doctors:
                raise BookingError("DOCTOR_UNAVAILABLE")
            preferences = preferences.model_copy(
                update={"doctor_ids": tuple(d.id for d in doctors)}
            )
        await self.service.store.release_call(state.call_id)
        state = state.model_copy(
            update={
                "phase": Phase.MOVE_OFFERED,
                "preferences": preferences,
                "move_proposal": None,
                "options": (),
                "authorization": None,
                "misunderstandings": 0,
            }
        )
        try:
            options = await self.service.options(
                state.client_id, state.move_target, preferences, call_id=state.call_id
            )
        except MisError as error:
            return mis_failure(state, turn, self.service.clock.now(), error)
        state = state.model_copy(update={"options": options})
        if not options:
            return state, reply(
                state,
                turn.turn_id,
                "Подходящих подтверждённых слотов нет. Прежняя запись сохраняется.",
                "Уточните предпочтения или попросите сотрудника.",
                code="NO_SLOTS",
            )
        doctors = {d.id: d.full_name for d in await self.names(state.move_target.specialty)}
        phrases = tuple(
            f"{i}. {self.slot_phrase(s, doctors.get(s.doctor_id))}"
            for i, s in enumerate(options, 1)
        )
        return state, reply(
            state, turn.turn_id, *phrases, "Какой вариант вам удобен?", code="MOVE_OPTIONS"
        )

    @staticmethod
    def slot_phrase(slot, doctor_name=None):
        from app.dialogue.visit_text import appointment_text

        day, at = appointment_text(slot.scheduled_at)
        doctor = f", {doctor_name}" if doctor_name else ""
        return f"{slot.specialty}{doctor}: {day}, {at}."

    def confirmation(self, state, turn):
        return state, reply(
            state,
            turn.turn_id,
            "Подтвердите перенос этой записи:"
            if state.move_proposal.stage == "move"
            else "Новая запись создана. Подтвердите завершение переноса:",
            CancellationConversation.visit_phrase(state.move_proposal.before),
            "Новое время:",
            self.slot_phrase(state.move_proposal.slot, state.move_proposal.doctor_name),
            room_text(state.move_proposal.room),
            "Прежнюю запись отменяем. Верно?",
            code="MOVE_CONFIRMATION_REQUIRED",
        )

    async def outcome(self, state, turn, action):
        if action.state == MoveState.RESCHEDULED:
            state = state.model_copy(
                update={
                    "phase": Phase.RESCHEDULED,
                    "move_proposal": None,
                    "move_target": None,
                    "visit_id": action.new_visit.id,
                    "authorization": None,
                    "options": (),
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Готово, запись перенесена.",
                self.slot_phrase(action.slot, action.new_visit.doctor.full_name),
                "Прежняя запись отменена. Могу ещё чем-то помочь?",
                code="RESCHEDULED",
            )
        if action.state == MoveState.NEW_CREATED:
            action = await self.service.store.renew(action.action_id, self.service.clock.now())
            state = state.model_copy(
                update={
                    "phase": Phase.MOVE_FINISH_CONFIRM,
                    "move_proposal": proposal(action, stage="finish"),
                    "authorization": None,
                    "options": (),
                }
            )
            return self.confirmation(state, turn)
        if action.state in {MoveState.REJECTED, MoveState.MANUAL}:
            return await self.transfer(
                state,
                turn,
                "R-06",
                reason="move_create_rejected"
                if action.state == MoveState.REJECTED
                else "move_manual",
            )
        state = state.model_copy(
            update={"phase": Phase.MOVE_PENDING, "authorization": None, "options": ()}
        )
        return state, reply(
            state,
            turn.turn_id,
            "Результат переноса уточняется. Скажите: проверьте перенос.",
            code="MOVE_PENDING",
        )

    async def step(self, state, turn, result):
        intents, slots = set(result.intents), result.slots
        identity_fields = {
            "last_name",
            "first_name",
            "middle_name",
            "birth_date",
        } & slots.model_fields_set
        if state.client_id is not None and (identity_fields or result.alternatives):
            if state.move_proposal:
                known = await self.service.store.get(state.move_proposal.action_id)
                if known.state not in {MoveState.HELD, MoveState.ABORTED, MoveState.EXPIRED}:
                    return await self.transfer(state, turn, "R-16")
            await self.service.store.release_call(state.call_id)
            state = state.model_copy(
                update={
                    "client_id": None,
                    "move_proposal": None,
                    "move_target": None,
                    "options": (),
                    "phase": Phase.IDENTIFYING,
                    "authorization": None,
                }
            )
        if state.move_proposal is not None:
            action = await self.service.store.get(state.move_proposal.action_id)
            if action.state in {MoveState.EXPIRED, MoveState.ABORTED}:
                state = state.model_copy(update={"move_proposal": None, "authorization": None})
                return await self.offer(state, turn, NluSlots())
            if action.state == MoveState.NEW_CREATED and state.phase != Phase.MOVE_FINISH_CONFIRM:
                return await self.outcome(state, turn, action)
            if action.state not in {MoveState.HELD, MoveState.NEW_CREATED}:
                state = state.model_copy(update={"phase": Phase.MOVE_PENDING})
            if (
                action.state in {MoveState.HELD, MoveState.NEW_CREATED}
                and action.expires_at <= self.service.clock.now()
            ):
                if action.state == MoveState.NEW_CREATED:
                    return await self.outcome(state, turn, action)
                await self.service.store.release_call(state.call_id)
                state = state.model_copy(update={"move_proposal": None})
                return await self.offer(state, turn, NluSlots())
        if state.phase == Phase.MOVE_PENDING:
            if "I-04" in intents or "проверь" in turn.text.casefold() or "repeat" in intents:
                action = await self.service.reconcile(state.move_proposal.action_id, state.call_id)
                return await self.outcome(state, turn, action)
            return await self.transfer(state, turn, "R-08")
        if state.client_id is None:
            if result.alternatives:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите дату рождения с четырёхзначным годом.",
                    code="BIRTH_DATE_AMBIGUOUS",
                )
            if slots.last_name and slots.first_name and slots.birth_date:
                try:
                    patient = await self.service.booking.identify(
                        slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
                    )
                except BookingError:
                    state = state.model_copy(
                        update={"identity_attempts": state.identity_attempts + 1}
                    )
                    if state.identity_attempts >= 2:
                        return await self.transfer(state, turn, "R-07")
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Не удалось однозначно найти пациента. Уточните ФИО и дату рождения.",
                        code="IDENTIFICATION_REQUIRED",
                    )
                return await self.identify_or_list(
                    state.model_copy(update={"client_id": patient.id}), turn
                )
        if state.phase == Phase.MOVE_CHOOSING:
            if "repeat" in intents or "I-02" in intents and not state.visit_items:
                return await self.identify_or_list(state, turn)
            if "choice" in intents and slots.choice_index is not None:
                if not 1 <= slots.choice_index <= len(state.visit_items):
                    raise BookingError("INVALID_CHOICE")
                return self.choose(state, turn, state.visit_items[slots.choice_index - 1])
            if slots.specialty or slots.visit_ref:
                return await self.identify_or_list(
                    state.model_copy(
                        update={"visit_query": query_from_slots(slots, state.visit_query)}
                    ),
                    turn,
                )
        if "no" in intents:
            if state.phase == Phase.MOVE_FINISH_CONFIRM:
                return await self.transfer(state, turn, "R-06", reason="move_manual")
            await self.service.store.release_call(state.call_id)
            state = await self.store.end(state.call_id)
            return state, reply(
                state, turn.turn_id, "Прежняя запись сохранена. Всего доброго!", code="CALL_ENDED"
            )
        if state.phase == Phase.MOVE_SOURCE and (
            "yes" in intents or "I-02" in intents or PREFERENCE_FIELDS & slots.model_fields_set
        ):
            return await self.offer(state, turn, slots)
        if state.phase == Phase.MOVE_OFFERED:
            if not state.options and "yes" in intents:
                return await self.offer(state, turn, slots)
            if "choice" in intents and slots.choice_index is not None:
                if not 1 <= slots.choice_index <= len(state.options):
                    raise BookingError("INVALID_CHOICE")
                slot = state.options[slots.choice_index - 1]
                room = self.rooms.room(slot.doctor_id, slot.scheduled_at) if self.rooms else None
                action = await self.service.prepare(
                    state.call_id, state.client_id, state.move_target, slot, room
                )
                state = state.model_copy(
                    update={
                        "phase": Phase.MOVE_CONFIRM,
                        "move_proposal": proposal(action),
                        "authorization": None,
                    }
                )
                return self.confirmation(state, turn)
            if (
                "repeat" in intents
                or "I-02" in intents
                or PREFERENCE_FIELDS & slots.model_fields_set
                or slots.doctor
            ):
                return await self.offer(state, turn, slots)
        if state.phase in {Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}:
            if "repeat" in intents:
                return self.confirmation(state, turn)
            if PREFERENCE_FIELDS & slots.model_fields_set or slots.doctor or slots.specialty:
                if state.phase == Phase.MOVE_FINISH_CONFIRM:
                    return await self.transfer(state, turn, "R-08")
                return await self.offer(state, turn, slots)
            if "yes" in intents and not intents & {"maybe", "opt_out"}:
                if not await self.store.authorize(
                    state.call_id, state.version, state.input_epoch, turn.turn_id
                ):
                    raise BookingError("CONFIRMATION_INTERRUPTED")
                try:
                    action = await self.service.confirm(
                        Approval(
                            call_id=state.call_id,
                            action_id=state.move_proposal.action_id,
                            revision=state.move_proposal.revision,
                            turn_id=turn.turn_id,
                            is_final=turn.is_final,
                            permitted=True,
                            guard_version=state.version,
                            input_epoch=state.input_epoch,
                        )
                    )
                except MisError as error:
                    action = await self.service.store.get(state.move_proposal.action_id)
                    if action.state == MoveState.NEW_CREATED:
                        state, answer = await self.outcome(state, turn, action)
                        waited, failed = mis_failure(state, turn, self.service.clock.now(), error)
                        return waited, failed.model_copy(
                            update={"utterances": answer.utterances + failed.utterances}
                        )
                    raise
                return await self.outcome(state, turn, action)
        if state.phase == Phase.RESCHEDULED and "I-02" in intents:
            return await self.start(state, turn, result)
        state = state.model_copy(update={"misunderstandings": state.misunderstandings + 1})
        if state.misunderstandings >= 3:
            return await self.transfer(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Уточните, какую запись и на какое время хотите перенести.",
            code="NOT_UNDERSTOOD",
        )
