"""Справка как короткое отступление: факты локальны, текущая операция не изменяется."""

from collections.abc import Callable

from app.dialogue.models import DialogueState, InformationReference, Phase, Reply, Turn, reply
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
    ):
        self.service, self.presenter, self.store = service, presenter, store
        self.continuation = continuation

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
        topic = self.service.find(turn.text, clarify_lab=state.information_clarifying)
        # Короткие ответы относятся к справочному вопросу, только пока он активен.
        answer = self.short_answer(turn.text)
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
        if topic is None:
            return None
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
        if topic == "lab_status":
            phrases += self.service.fact("lab_terms").utterances
        elif not clarifying:
            phrases += self._continuation(state, turn)
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
                    topic=topic,
                    sections=fact.source,
                )
            }
        )

    def _continuation(self, state: DialogueState, turn: Turn) -> tuple[str, ...]:
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
