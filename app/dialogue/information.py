"""Справка как короткое отступление: факты локальны, текущая операция не изменяется."""

import re
from collections.abc import Awaitable, Callable

from app.booking.preferences import SlotPreferences
from app.dialogue.introduction import classify_introduction
from app.dialogue.models import (
    DialogueState,
    InformationReference,
    Phase,
    Reply,
    TransferRequest,
    Turn,
    reply,
)
from app.dialogue.presentation import BookingPresenter
from app.dialogue.visits import VisitConversation
from app.information.catalog import InformationService
from app.nlu.lexicon import Pattern, tokenize
from app.nlu.parsers.answers import parse_answer

# Эти состояния не ожидают согласия на изменение МИС.
IDLE = {Phase.READY, Phase.BOOKED, Phase.CANCELLED, Phase.RESCHEDULED}
LABORATORY_QUESTION = Pattern.parse("анализ|результат|холестерин|глюкоза|гемоглобин|лейкоцит")
RESULT_EVALUATION = Pattern.parse("все + нормально|хороший|плохой|порядок")
SHORT_ANSWER_WORDS = {
    "да",
    "ага",
    "угу",
    "конечно",
    "хорошо",
    "нет",
    "спасибо",
    "не",
    "нужно",
    "надо",
    "повторите",
    "повтори",
    "пожалуйста",
    "еще",
    "раз",
    "все",
    "понятно",
}


class InformationConversation:
    def __init__(
        self,
        service: InformationService,
        presenter: BookingPresenter,
        store,
        continuation: Callable[[DialogueState, Turn], tuple[str, ...]],
        release_call_holds: Callable[[str], Awaitable[None]] | None = None,
        transfer: Callable[[DialogueState, Turn, str], Awaitable[tuple[DialogueState, Reply]]]
        | None = None,
    ):
        self.service, self.presenter, self.store = service, presenter, store
        self.continuation = continuation
        self.release_call_holds = release_call_holds
        self.transfer = transfer

    @staticmethod
    def short_answer(text: str) -> str | None:
        return parse_answer(text) if set(tokenize(text)) <= SHORT_ANSWER_WORDS else None

    @staticmethod
    def medical_followup(state: DialogueState, turn: Turn) -> bool:
        topic = state.information_topic or ""
        return bool(
            (topic.startswith("lab") or state.information_clarifying)
            and RESULT_EVALUATION.find(tokenize(turn.text), 12)
        )

    def laboratory_boundary(self, result: Reply, turn: Turn, *, contextual: bool) -> Reply:
        if result.code != "MEDICAL_BOUNDARY" or not (
            contextual or LABORATORY_QUESTION.find(tokenize(turn.text), 24)
        ):
            return result
        # Предложение записи остаётся последним: последующее «да» отвечает именно на него.
        fact = self.service.fact("lab_values")
        collection = self.service.fact("lab_collection")
        return result.model_copy(
            update={
                "utterances": fact.utterances + collection.utterances + result.utterances,
                "information": InformationReference(
                    document=self.service.catalog.document,
                    version=self.service.catalog.version,
                    topic="lab_values",
                    sections=tuple(dict.fromkeys((*fact.source, *collection.source))),
                ),
            }
        )

    async def step(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply] | None:
        introduction = classify_introduction(turn.text)
        topic = self.service.find(turn.text, clarify_lab=state.information_clarifying)
        if state.purpose == "reschedule" and state.phase not in IDLE and topic == "choose_doctor":
            # A concrete doctor or date change during a move belongs to that task.
            topic = None
        if introduction == "booking_abandoned" and topic is not None:
            if (
                state.purpose == "booking"
                and state.phase not in IDLE
                and state.phase
                not in {Phase.RECONCILING, Phase.CANCEL_RECONCILING, Phase.MOVE_PENDING}
            ):
                # Отказ и новый справочный вопрос в одной реплике: освобождаем
                # только локальную бронь и отвечаем на вопрос, не продолжая сбор ФИО.
                if self.release_call_holds is not None:
                    await self.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.READY,
                        "purpose": "booking",
                        "client_id": None,
                        "specialty": None,
                        "preferences": SlotPreferences(),
                        "options": (),
                        "proposal": None,
                        "authorization": None,
                        "shown_option_keys": (),
                        "identity_attempts": 0,
                        "identity_draft": None,
                        "misunderstandings": 0,
                        "last_question": None,
                        "suspended_booking": None,
                        "information_topic": None,
                        "information_clarifying": False,
                    }
                )
            introduction = None
        if introduction == "booking_abandoned":
            if state.purpose != "booking" or state.phase in IDLE:
                introduction = "general_help"
            elif state.phase in {
                Phase.RECONCILING,
                Phase.CANCEL_RECONCILING,
                Phase.MOVE_PENDING,
            }:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Конечно. Результат прежней операции ещё уточняется; повторять её не буду.",
                    "Какой общий вопрос вас интересует?",
                    code="BOOKING_TASK_PENDING",
                )
            else:
                # Отказ от намерения записи освобождает только локальную бронь.
                # Неопределённые внешние операции выше намеренно не сбрасываются.
                if self.release_call_holds is not None:
                    await self.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.READY,
                        "purpose": "booking",
                        "client_id": None,
                        "specialty": None,
                        "preferences": SlotPreferences(),
                        "options": (),
                        "proposal": None,
                        "authorization": None,
                        "shown_option_keys": (),
                        "identity_attempts": 0,
                        "identity_draft": None,
                        "misunderstandings": 0,
                        "last_question": None,
                        "suspended_booking": None,
                        "information_topic": None,
                        "information_clarifying": False,
                    }
                )
                return state, reply(
                    state,
                    turn.turn_id,
                    "Конечно. Не будем продолжать запись.",
                    "Могу ответить на общий вопрос. Что именно вас интересует?",
                    code="BOOKING_TASK_ABANDONED",
                )
        if introduction is not None:
            phrases = {
                "capabilities": (
                    "Я виртуальный помощник регистратуры. Могу помочь с записью,"
                    " проверкой ваших приёмов, отменой или переносом записи и общей справкой.",
                    "Если нужен сотрудник, скажите «оператор».",
                ),
                "booking_guidance": (
                    "Чтобы начать запись, скажите, к какому специалисту хотите попасть.",
                    "Я сначала проверю расписание и условия приёма. Личные данные понадобятся"
                    " только для проверки доступности записи.",
                ),
                "general_help": (
                    "Могу подсказать часы работы, адрес, расписание врача, информацию об анализах"
                    " или порядок записи.",
                    "Что именно вас интересует?",
                ),
                "unsupported_service": (
                    "В каталоге помощника нет расписания массажиста, поэтому оформить такую запись"
                    " автоматически я не могу.",
                    "Могу помочь с записью к врачу, общей справкой или запросом сотруднику.",
                ),
            }[introduction]
            continuation = self.resume_question(state, turn)
            if state.phase in IDLE:
                continuation = () if introduction == "general_help" else continuation
            codes = {
                "capabilities": "CAPABILITIES_GIVEN",
                "booking_guidance": "BOOKING_GUIDANCE_GIVEN",
                "general_help": "GENERAL_HELP_REQUIRED",
                "unsupported_service": "SERVICE_UNAVAILABLE",
            }
            return state, reply(
                state,
                turn.turn_id,
                *phrases,
                *continuation,
                code=codes[introduction],
            )
        # Короткие ответы относятся к справочному вопросу, только пока он активен.
        answer = self.short_answer(turn.text)
        tokens = tokenize(turn.text)
        if (
            state.information_topic is not None
            and state.phase in IDLE
            and tokens
            and tokens[0] in {"нет", "не"}
            and set(tokens[1:])
            <= {"спасибо", "я", "сам", "сама", "самостоятельно", "на", "госуслугах"}
        ):
            answer = "no"
        if (
            state.information_topic == "tax_certificate"
            and not state.information_clarifying
            and answer == "yes"
            and self.transfer is not None
        ):
            return await self.transfer(state, turn, "R-10")
        if state.information_topic == "lab_status" and re.search(
            r"\bкак\b.*\bузна\w*", turn.text.casefold()
        ):
            topic = "lab_collection"
        if (
            topic is None
            and state.information_topic is not None
            and state.phase in IDLE
            and not state.medical_boundary_pending
        ):
            if answer == "repeat":
                topic = state.information_topic
            elif not state.information_clarifying and answer == "no":
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            elif not state.information_clarifying and answer == "yes":
                state = VisitConversation.booking_state(state).model_copy(
                    update={"information_topic": None}
                )
                return state, reply(
                    state, turn.turn_id, "Чем ещё могу помочь?", code="MORE_HELP_REQUIRED"
                )
        if (
            topic is None
            and state.information_topic is None
            and state.phase in IDLE
            and "спасибо" in tokens
            and set(tokens) <= {"хорошо", "спасибо", "говорю", "большое", "вам", "тебе"}
        ):
            return state, reply(
                state,
                turn.turn_id,
                "Пожалуйста. Могу ещё чем-то помочь?",
                code="ACKNOWLEDGED",
            )
        if topic is None:
            return None
        if topic == "vaccine_availability" and self.transfer is not None:
            fact = self.service.fact(topic)
            if self.release_call_holds is not None:
                await self.release_call_holds(state.call_id)
            transfer = TransferRequest(route="R-08", priority="P3")
            state = state.model_copy(
                update={
                    "phase": Phase.HANDOFF,
                    "transfer": transfer,
                    "authorization": None,
                    "proposal": None,
                    "options": (),
                    "identity_draft": None,
                }
            )
            result = reply(
                state,
                turn.turn_id,
                fact.utterances[0],
                "Соединяю с регистратором, чтобы уточнить наличие вакцины.",
                code="TRANSFER_REQUESTED",
            )
            return state, result.model_copy(
                update={
                    "information": InformationReference(
                        document=self.service.catalog.document,
                        version=self.service.catalog.version,
                        topic=topic,
                        sections=fact.source,
                    )
                }
            )
        # Повтор уточняющего вопроса не запускает бесконечное уточнение.
        if topic == "lab_terms" and state.information_clarifying:
            topic = "lab_unknown"
        clarifying = topic in {"lab_terms", "lab_status"}
        state = state.model_copy(
            update={
                "information_topic": topic if clarifying or state.phase in IDLE else None,
                "information_clarifying": clarifying,
                "authorization": None,
                "misunderstandings": 0,
            }
        )
        fact = self.service.fact(topic)
        phrases = fact.utterances
        reference_topic = topic
        if topic == "lab_status":
            relative_request = re.search(
                r"\b(мам\w*|матер\w*|пап\w*|отц\w*|бабуш\w*|дедуш\w*|доч\w*|сын\w*)\b",
                turn.text.casefold().replace("ё", "е"),
            ) and re.search(r"\b(мой|моя|мое|моего|моей|мою|у|за)\b", turn.text.casefold())
            if relative_request:
                collection = self.service.fact("lab_collection")
                phrases = (
                    "Эти сведения я могу сообщить только самому пациенту.",
                    *collection.utterances,
                )
                fact = collection
                reference_topic = "lab_collection"
            else:
                phrases += self.service.fact("lab_terms").utterances
        elif not clarifying:
            phrases += self.resume_question(state, turn)
        result = reply(
            state,
            turn.turn_id,
            *phrases,
            code="INFORMATION_REQUIRED"
            if topic == "lab_terms"
            else "INFO_UNAVAILABLE"
            if topic in {"lab_status", "lab_unknown", "holidays"}
            else "INFO_GIVEN",
        )
        return state, result.model_copy(
            update={
                "information": InformationReference(
                    document=self.service.catalog.document,
                    version=self.service.catalog.version,
                    topic=reference_topic,
                    sections=fact.source,
                )
            }
        )

    def resume_question(self, state: DialogueState, turn: Turn) -> tuple[str, ...]:
        if state.medical_boundary_pending:
            return ("Записать вас к терапевту?",)
        if state.phase in IDLE:
            return ("Могу ещё чем-то помочь?",)
        if state.phase == Phase.IDENTIFYING:
            return (
                "Теперь назовите дату рождения пациента."
                if state.identity_draft
                else self.presenter.ask_patient(),
            )
        confirmation = self.continuation(state, turn)
        if confirmation:
            return confirmation
        if state.phase in {Phase.OFFERED, Phase.MOVE_OFFERED}:
            return ("Какой из предложенных вариантов вам подходит?",)
        if state.phase in {Phase.RECONCILING, Phase.CANCEL_RECONCILING, Phase.MOVE_PENDING}:
            return ("Результат прежней операции ещё уточняется. Повторно отправлять её не буду.",)
        if state.phase in {Phase.CANCEL_CHOOSING, Phase.MOVE_CHOOSING}:
            return ("Какую из ваших записей нужно выбрать?",)
        if state.phase == Phase.VERIFYING_OWNER:
            return ("Вы звоните о своей записи?",)
        if state.phase == Phase.MOVE_EMPTY:
            return ("Хотите оформить новую запись?",)
        if state.phase == Phase.VISITS_SHOWN:
            return (
                "Показать следующие записи?"
                if state.visit_has_more
                else "Уточните, что ещё хотите узнать о записи, или скажите «нет», чтобы закончить.",
            )
        if state.phase == Phase.IDENTIFIED:
            return (
                "Назовите удобный день или время."
                if state.specialty
                else "К какому врачу хотите записаться?",
            )
        # Другие ветки продолжаются существующим обработчиком, без изменения их параметров.
        return ("Чтобы продолжить прежний вопрос, скажите «повторите» или назовите новую просьбу.",)
