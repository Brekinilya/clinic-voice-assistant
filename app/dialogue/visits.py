"""С-05: идентификация, собственные будущие записи и последовательные страницы справки."""

import re
from collections.abc import Awaitable, Callable

from app.booking.models import BookingError
from app.booking.preferences import PREFERENCE_FIELDS, SlotPreferences
from app.booking.service import BookingService
from app.contracts import NluResult, NluSlots
from app.dialogue.failures import mis_failure
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.dialogue.ports import DialogueStore
from app.dialogue.visit_privacy import asks_for_sms, requested_owner
from app.dialogue.visit_text import appointment_text, room_text
from app.mis.ports import MisError
from app.visits.models import VisitQuery
from app.visits.service import VisitService

TransferHandler = Callable[[DialogueState, Turn, str], Awaitable[tuple[DialogueState, Reply]]]


def wants_new_booking(text: str) -> bool:
    return (
        re.search(
            r"\b(?:запишите|записаться|записать|запишусь|запиши|нужен\s+талон|новую\s+запись)\b",
            text.casefold(),
        )
        is not None
    )


def query_from_slots(slots: NluSlots, previous: VisitQuery | None = None) -> VisitQuery:
    previous = previous if previous is not None else VisitQuery()
    preferences = previous.preferences.model_dump()
    preferences.update(
        {name: getattr(slots, name) for name in PREFERENCE_FIELDS if name in slots.model_fields_set}
    )
    specialty = slots.specialty if "specialty" in slots.model_fields_set else previous.specialty
    doctor = slots.doctor if "doctor" in slots.model_fields_set else previous.doctor
    if (
        slots.specialty is not None
        and slots.specialty != previous.specialty
        and slots.doctor is None
    ):
        doctor = None
    if slots.doctor is not None and slots.specialty is None:
        specialty = None
    if slots.visit_ref is not None:
        ref = slots.visit_ref
        specialty, doctor = ref.specialty or specialty, ref.doctor or doctor
        if ref.date is not None:
            preferences.update(date_from=ref.date, date_to=ref.date)
        if ref.time is not None:
            preferences.update(time_from=ref.time, time_to=ref.time)
    return VisitQuery(
        specialty=specialty, doctor=doctor, preferences=SlotPreferences(**preferences)
    )


class VisitConversation:
    def __init__(
        self,
        booking: BookingService,
        store: DialogueStore,
        visits: VisitService,
        transfer: TransferHandler,
    ) -> None:
        self.booking, self.store, self.visits, self.transfer = booking, store, visits, transfer

    async def start(
        self, state: DialogueState, turn: Turn, result: NluResult
    ) -> tuple[DialogueState, Reply]:
        owner = requested_owner(turn.text)
        if result.slots.on_behalf in {"other", "child"} or owner == "other":
            return await self.privacy_transfer(state, turn)
        own = result.slots.on_behalf == "self" or owner == "self"
        state = state.model_copy(
            update={
                "purpose": "visits",
                "query_for_self": own,
                "visit_query": query_from_slots(result.slots),
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "proposal": None,
                "options": (),
                "authorization": None,
                "identity_attempts": 0,
                "misunderstandings": 0,
                "visit_id": None,
            }
        )
        if not own:
            state = state.model_copy(update={"phase": Phase.VERIFYING_OWNER, "client_id": None})
            return state, reply(
                state,
                turn.turn_id,
                "Сведения доступны самому пациенту. Вы звоните о своей записи?",
                code="OWNER_CONFIRMATION_REQUIRED",
            )
        return await self.identify_or_show(state, turn)

    async def identify_or_show(self, state: DialogueState, turn: Turn):
        if state.client_id is None:
            state = state.model_copy(update={"phase": Phase.IDENTIFYING})
            return state, reply(
                state,
                turn.turn_id,
                "Назовите фамилию, имя, отчество и дату рождения.",
                code="IDENTIFICATION_REQUIRED",
            )
        return await self.show(state, turn)

    async def privacy_transfer(self, state: DialogueState, turn: Turn):
        state, _ = await self.transfer(state, turn, "R-16")
        return state, reply(
            state,
            turn.turn_id,
            "Сведения о записях могу сообщить только самому пациенту.",
            "Проверку полномочий представителя выполнит сотрудник. Запрос подготовлен.",
            code="PRIVACY_RESTRICTED",
        )

    async def show(self, state: DialogueState, turn: Turn, *, next_page: bool = False):
        if state.client_id is None or not state.query_for_self:
            raise BookingError("IDENTIFICATION_REQUIRED")
        try:
            page = await self.visits.page(
                state.client_id,
                state.visit_query,
                after=state.visit_cursor if next_page else None,
            )
        except MisError as error:
            # Не выдаём сохранённую страницу за свежий ответ при отказе чтения.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        state = state.model_copy(
            update={
                "phase": Phase.VISITS_SHOWN,
                "visit_items": page.items,
                "visit_cursor": page.cursor,
                "visit_has_more": page.has_more,
                "misunderstandings": 0,
            }
        )
        if not page.items:
            return state, reply(
                state,
                turn.turn_id,
                "Других подходящих будущих записей нет."
                if next_page
                else "Подходящих будущих записей не найдено. Хотите записаться?",
                code="NO_MORE_VISITS" if next_page else "NO_ACTIVE_VISITS",
            )
        phrases = []
        for item in page.items:
            phrases.extend(
                (
                    f"{item.specialty}, {item.doctor_name}.",
                    *appointment_text(item.scheduled_at),
                    room_text(item.room),
                )
            )
        phrases.append(
            "Есть ещё записи. Рассказать следующие?" if page.has_more else "Нужна ещё помощь?"
        )
        return state, reply(state, turn.turn_id, *phrases, code="INFO_GIVEN")

    @staticmethod
    def booking_state(state: DialogueState) -> DialogueState:
        return state.model_copy(
            update={
                "purpose": "booking",
                "phase": Phase.IDENTIFIED if state.client_id is not None else Phase.READY,
                "specialty": None,
                "preferences": SlotPreferences(),
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "query_for_self": False,
                "visit_query": VisitQuery(),
                "misunderstandings": 0,
            }
        )

    async def step(self, state: DialogueState, turn: Turn, result: NluResult):
        slots, intents = result.slots, set(result.intents)
        if slots.on_behalf in {"other", "child"} or requested_owner(turn.text) == "other":
            return await self.privacy_transfer(state, turn)
        if asks_for_sms(turn.text):
            return state, reply(
                state,
                turn.turn_id,
                "Отправка SMS пока не подключена. Сообщение не отправлено.",
                "Вы звоните о своей записи?"
                if state.phase == Phase.VERIFYING_OWNER
                else "Могу помочь в текстовом диалоге или подготовить запрос сотруднику.",
                code="SMS_UNAVAILABLE",
            )
        if state.phase == Phase.VERIFYING_OWNER:
            if "yes" in intents:
                state = state.model_copy(update={"query_for_self": True, "misunderstandings": 0})
                return await self.identify_or_show(state, turn)
            if "no" in intents:
                return await self.privacy_transfer(state, turn)
        if result.alternatives:
            state = state.model_copy(update={"phase": Phase.IDENTIFYING, "client_id": None})
            return state, reply(
                state,
                turn.turn_id,
                "Дата рождения неоднозначна. Назовите ФИО и дату рождения с четырёхзначным годом.",
                code="BIRTH_DATE_AMBIGUOUS",
            )
        if slots.policy_number is not None:
            return await self.transfer(state, turn, "R-07")
        if (
            state.phase == Phase.IDENTIFYING
            and slots.last_name
            and slots.first_name
            and slots.birth_date
        ):
            try:
                patient = await self.booking.identify(
                    slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
                )
            except MisError as error:
                return mis_failure(state, turn, self.booking.clock.now(), error)
            except BookingError:
                attempts = state.identity_attempts + 1
                if attempts >= 2:
                    return await self.transfer(state, turn, "R-07")
                state = state.model_copy(update={"identity_attempts": attempts, "client_id": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Пациент не найден однозначно. Повторите полные ФИО и дату рождения.",
                    code="IDENTIFICATION_REQUIRED",
                )
            state = state.model_copy(
                update={"client_id": patient.id, "identity_attempts": 0, "misunderstandings": 0}
            )
            return await self.show(state, turn)
        if "I-04" in intents:
            return await self.start(state, turn, result)
        if state.phase == Phase.VISITS_SHOWN:
            if slots.model_fields_set & {
                "specialty",
                "doctor",
                "date_from",
                "date_to",
                "time_from",
                "time_to",
                "exclude_weekdays",
                "exclude_dates",
                "visit_ref",
            }:
                state = state.model_copy(
                    update={
                        "visit_query": query_from_slots(slots, state.visit_query),
                        "visit_cursor": None,
                    }
                )
                return await self.show(state, turn)
            if "no" in intents:
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            if "yes" in intents and state.visit_has_more:
                return await self.show(state, turn, next_page=True)
            if "repeat" in intents:
                return await self.show(state, turn)
            if "yes" in intents and not state.visit_items:
                state = self.booking_state(state)
                return state, reply(state, turn.turn_id, "К какому специалисту хотите записаться?")
        count = state.misunderstandings + 1
        state = state.model_copy(update={"misunderstandings": count})
        if count >= 3:
            return await self.transfer(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Уточните, пожалуйста, просьбу."
            if count == 1
            else "Не удалось понять. Могу подготовить запрос регистратору.",
            code="NOT_UNDERSTOOD",
        )
