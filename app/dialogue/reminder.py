"""С-10: ограниченный исходящий разговор поверх существующих С-03/С-04.

Проверка адресата не разрешает изменение МИС. Диалог никогда не перечисляет другие
визиты клиента: его полномочия ограничены исходным визитом доверенной попытки.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.booking.models import Approval, BookingError
from app.booking.preferences import PREFERENCE_FIELDS, SlotPreferences
from app.cancellation.models import CancelState
from app.contracts import NluSlots
from app.dialogue.cancellation import CancellationConversation
from app.dialogue.cross_scenario import CrossScenario
from app.dialogue.cross_scenario import classify as classify_cross_scenario
from app.dialogue.failures import mis_failure
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.dialogue.outbound import (
    MarkOperation,
    MarkStatus,
    ReminderActions,
    ReminderOutcome,
    ReminderResult,
    mark_action_id,
)
from app.dialogue.visit_privacy import mentions_existing_visits
from app.mis.ports import MisError
from app.nlu.lexicon import normalize
from app.nlu.parsers.answers import parse_answer
from app.recovery.models import OwnershipLost
from app.rescheduling.models import MoveState

if TYPE_CHECKING:
    from app.dialogue.natural import BookingConversation

VERIFY_YES = {"да", "да это я", "это я", "я", "верно", "да верно"}
ATTEND_YES = {
    "да",
    "приду",
    "да приду",
    "буду",
    "да буду",
    "обязательно приду",
    "да обязательно приду",
    "подтверждаю",
    "подтверждаю запись",
    "да подтверждаю",
    "да все верно",
    "все верно",
}
OPT_OUT = re.compile(
    r"\b(?:не (?:звоните|беспокойте)|не (?:надо|нужно) (?:мне )?(?:больше )?звонить|"
    r"уберите (?:мой )?номер|отказ(?:ываюсь)? от обзвона|"
    r"отключите (?:обзвон|напоминания)|не хочу получать напоминания)\b"
)
WRONG_PERSON = re.compile(r"\b(?:не я|не он|не она|ошиблись|ошиблись номером|другой человек)\b")
GOODBYE = {"до свидания", "всего доброго", "пока", "закончим разговор", "не хочу разговаривать"}
DECLINE = {"нет", "не приду", "не буду", "не смогу прийти", "не получится", "не могу прийти"}


def words(text: str) -> str:
    return " ".join(re.findall(r"[а-яa-z0-9]+", normalize(text)))


class ReminderConversation:
    def __init__(self, conversation: BookingConversation, actions: ReminderActions):
        self.conversation, self.actions = conversation, actions
        self.store = conversation.store
        # Проверяется повторно после всех ожиданий, непосредственно перед началом записи.
        if conversation.cancellation is not None:
            if conversation.cancellation.cancellation.permission is not None:
                raise ValueError("дополнительное разрешение отмены уже настроено")
            conversation.cancellation.cancellation.permission = self.guard_existing_write
        if conversation.rescheduling is not None:
            if conversation.rescheduling.service.permission is not None:
                raise ValueError("дополнительное разрешение переноса уже настроено")
            conversation.rescheduling.service.permission = self.guard_existing_write

    async def guard_existing_write(self, approval: Approval) -> None:
        state = await self.store.inspect(approval.call_id)
        if state.reminder is None:
            return
        session = state.reminder
        target = state.cancel_target if state.purpose == "cancel" else state.move_target
        if (
            not session.verified
            or state.client_id != session.binding.client_id
            or target is None
            or target.model_copy(update={"room": None})
            != session.binding.visit.model_copy(update={"room": None})
        ):
            raise BookingError("CONFIRMATION_INTERRUPTED")
        await self.actions.check_attempt(session.binding)

    def prompt(self, state: DialogueState) -> tuple[str, ...]:
        if state.phase == Phase.REMIND_VERIFY:
            return ("Это пациент, которому я звоню? Ответьте «да» или «нет».",)
        if state.phase == Phase.REMIND_DETAILS:
            if state.reminder.declined:
                return ("Могу перенести запись вместо отмены. Перенести или отменить?",)
            return (
                CancellationConversation.visit_phrase(state.reminder.binding.visit),
                "Вы придёте на приём?",
            )
        if state.phase in {Phase.CANCEL_RECONCILING, Phase.MOVE_PENDING}:
            return ("Результат операции ещё уточняется. Скажите «проверьте результат».",)
        return self.conversation.information_continuation(
            state, Turn(turn_id=1, text="повторите")
        ) or ("Какой из предложенных вариантов вам подходит?",)

    async def finish(
        self,
        state: DialogueState,
        turn: Turn,
        outcome: ReminderOutcome,
        *phrases: str,
        status: MarkStatus | None = None,
    ) -> tuple[DialogueState, Reply]:
        await self.conversation.release_call(state.call_id)
        state = state.model_copy(
            update={
                "phase": Phase.ENDED,
                "authorization": None,
                "client_id": None,
                "options": (),
                "proposal": None,
                "cancel_proposal": None,
                "cancel_target": None,
                "move_proposal": None,
                "move_target": None,
                "visit_items": (),
                "reminder_result": ReminderResult(
                    attempt_id=state.reminder.binding.attempt_id,
                    outcome=outcome,
                    mis_status=status or "not_required",
                ),
            }
        )
        return state, reply(state, turn.turn_id, *phrases, code=outcome)

    async def handoff(self, state, turn, route="R-08"):
        state, result = await self.conversation.transfer(state, turn, route)
        return state, result.model_copy(
            update={
                "reminder_result": ReminderResult(
                    attempt_id=state.reminder.binding.attempt_id,
                    outcome="ESCALATED",
                )
            }
        )

    async def _mark(self, state, turn, operation: MarkOperation):
        binding = state.reminder.binding
        if not await self.store.authorize(
            state.call_id,
            state.version,
            state.input_epoch,
            turn.turn_id,
            reminder_operation=operation,
        ):
            raise BookingError("CONFIRMATION_INTERRUPTED")
        approval = Approval(
            call_id=state.call_id,
            action_id=mark_action_id(binding, operation),
            revision=binding.fence,
            turn_id=turn.turn_id,
            is_final=True,
            permitted=True,
            guard_version=state.version,
            input_epoch=state.input_epoch,
        )
        await self.actions.check_attempt(binding)
        await self.store.check_reminder_guard(approval, binding, operation)
        status = await getattr(self.actions, operation)(binding, approval)
        if operation == "confirm":
            text = (
                "Спасибо, ждём вас на приёме."
                if status == "applied"
                else ("Спасибо. Ваш ответ сохранён; отметка в системе ещё требует проверки.")
            )
            return await self.finish(state, turn, "CONFIRMED", text, status=status)
        text = (
            "Отказ от обзвона сохранён. Всего доброго."
            if status == "applied"
            else ("Отказ от обзвона сохранён локально. Отметка в системе ещё требует проверки.")
        )
        return await self.finish(state, turn, "OPT_OUT", text, status=status)

    async def _pending(self, state) -> bool:
        if state.cancel_proposal and self.conversation.cancellation:
            action = await self.conversation.cancellation.cancellation.store.get(
                state.cancel_proposal.action_id
            )
            if action.state in {
                CancelState.COMMITTING,
                CancelState.RECONCILING,
                CancelState.CANCELLED,
            }:
                return True
        if state.move_proposal and self.conversation.rescheduling:
            action = await self.conversation.rescheduling.service.store.get(
                state.move_proposal.action_id
            )
            if action.state not in {MoveState.HELD, MoveState.ABORTED, MoveState.EXPIRED}:
                return True
        return False

    async def _details(self, state, turn):
        await self.conversation.release_call(state.call_id)
        state = state.model_copy(
            update={
                "phase": Phase.REMIND_DETAILS,
                "purpose": "booking",
                "authorization": None,
                "cancel_target": None,
                "cancel_proposal": None,
                "move_target": None,
                "move_proposal": None,
                "options": (),
                "misunderstandings": 0,
                "reminder": state.reminder.model_copy(update={"declined": False}),
            }
        )
        return state, reply(state, turn.turn_id, "Прежняя запись сохранена.", *self.prompt(state))

    async def _start_change(self, state, turn, result, *, cancel: bool):
        await self.conversation.release_call(state.call_id)
        state = state.model_copy(
            update={
                "authorization": None,
                "options": (),
                "cancel_proposal": None,
                "move_proposal": None,
                "cancel_target": None,
                "move_target": None,
                "visit_items": (),
                "preferences": SlotPreferences(),
                "misunderstandings": 0,
                "purpose": "cancel" if cancel else "reschedule",
            }
        )
        target = state.reminder.binding.visit
        if cancel:
            handler = self.conversation.cancellation
            if handler is None:
                return await self.handoff(state, turn)
            state = state.model_copy(update={"cancel_reschedule_offered": state.reminder.declined})
            return await handler.choose(state, turn, target)
        handler = self.conversation.rescheduling
        if handler is None:
            return await self.handoff(state, turn)
        state, answer = handler.choose(state, turn, target)
        if PREFERENCE_FIELDS & result.slots.model_fields_set or result.slots.doctor:
            return await handler.offer(state, turn, result.slots)
        return state, answer

    async def step(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        try:
            await self.actions.check_attempt(state.reminder.binding)
            return await self._step(state, turn)
        except OwnershipLost:
            return await self.finish(
                state,
                turn,
                "ESCALATED",
                "Эта попытка звонка уже завершена. Обратитесь в регистратуру.",
            )
        except MisError as error:
            return mis_failure(state, turn, self.conversation.booking.clock.now(), error)
        except BookingError as error:
            if str(error) == "CONFIRMATION_INTERRUPTED":
                return state, reply(
                    state,
                    turn.turn_id,
                    "Поступила новая реплика. Подтвердите ваш выбор заново.",
                    code="TURN_INTERRUPTED",
                )
            return await self.handoff(state, turn, "R-06")

    async def _step(self, state, turn):
        text, answer = words(turn.text), parse_answer(turn.text)
        cross_scenario = classify_cross_scenario(turn.text)
        if cross_scenario == CrossScenario.THIRD_PARTY:
            return await self.handoff(state, turn, "R-16")
        if state.phase == Phase.REMIND_VERIFY:
            if OPT_OUT.search(text):
                # Номер нельзя продолжать обзванивать, но третье лицо не меняет карточку пациента.
                await self.actions.stop_calls(state.reminder.binding)
                return await self.finish(
                    state,
                    turn,
                    "OPT_OUT",
                    "Звонки на этот контакт отключены. Отметку в карточке пациента должен проверить сотрудник.",
                    status="manual",
                )
            if text in VERIFY_YES:
                state = state.model_copy(
                    update={
                        "phase": Phase.REMIND_DETAILS,
                        "client_id": state.reminder.binding.client_id,
                        "reminder": state.reminder.model_copy(update={"verified": True}),
                        "misunderstandings": 0,
                    }
                )
                return state, reply(
                    state, turn.turn_id, *self.prompt(state), code="REMINDER_DETAILS"
                )
            if answer == "no" or WRONG_PERSON.search(text):
                return await self.finish(
                    state, turn, "WRONG_PERSON", "Извините за беспокойство. Всего доброго."
                )
        else:
            if not state.reminder.verified or state.client_id != state.reminder.binding.client_id:
                return await self.handoff(state, turn)
            if OPT_OUT.search(text):
                return await self._mark(state, turn, "opt_out")
            if WRONG_PERSON.search(text):
                if await self._pending(state):
                    return await self.handoff(state, turn, "R-16")
                return await self.finish(state, turn, "WRONG_PERSON", "Всего доброго.")

        if text in GOODBYE:
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            return await self.finish(state, turn, "MAYBE", "Всего доброго.")
        if answer == "repeat" and state.phase not in {Phase.CANCEL_RECONCILING, Phase.MOVE_PENDING}:
            return state, reply(state, turn.turn_id, *self.prompt(state), code="REMINDER_REPEATED")
        if (
            classify_cross_scenario(turn.text) == CrossScenario.OTHER_DAY
            and state.purpose == "reschedule"
            and state.phase in {Phase.MOVE_SOURCE, Phase.MOVE_OFFERED, Phase.MOVE_CONFIRM}
        ):
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо. Какой новый день или время вам подходит?",
                code="OTHER_DAY_REQUESTED",
            )
        if re.search(
            r"\b(?:сколько|какая|какова)\b.*\b(?:стоит|стоимость|цена|цену)\b|\bцена\b.*\bпри[её]м\b",
            text,
        ):
            return state, reply(
                state,
                turn.turn_id,
                "Точную стоимость приёма не могу назвать; её уточнит регистратор.",
                *self.prompt(state),
                code="PRICE_UNAVAILABLE",
            )
        flags = self.conversation.safety.evaluate(state.call_id, turn).flags
        if flags & {"robot", "no_recording"}:
            state = state.model_copy(update={"authorization": None})
            if "no_recording" in flags:
                state = state.model_copy(update={"recording_opt_out": True})
            return state, reply(
                state,
                turn.turn_id,
                "Я виртуальный помощник регистратуры."
                if "robot" in flags
                else "Запись разговора отключена.",
                *self.prompt(state),
                code="REMINDER_SERVICE",
            )
        if "medical" in flags:
            return await self.handoff(state, turn, "R-04")
        if cross_scenario == CrossScenario.GOODBYE:
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            return await self.finish(state, turn, "MAYBE", "Всего доброго.")
        if cross_scenario == CrossScenario.WAIT:
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо, я подожду.",
                *self.prompt(state),
                code="REMINDER_WAITING",
            )
        topic = self.conversation.information.service.find(turn.text)
        if topic and topic not in {"lab_status", "lab_terms"}:
            fact = self.conversation.information.service.fact(topic)
            return state, reply(
                state, turn.turn_id, *fact.utterances, *self.prompt(state), code="INFO_GIVEN"
            )
        if state.phase == Phase.REMIND_VERIFY:
            return await self.unknown(state, turn)

        # Короткий ответ не расходует бюджет даже при отсутствующем каталоге врачей.
        if state.phase == Phase.REMIND_DETAILS and text in ATTEND_YES:
            if not state.reminder.declined:
                return await self._mark(state, turn, "confirm")
            return await self.unknown(state, turn)
        if answer == "maybe":
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            if state.misunderstandings >= 1:
                return await self.handoff(state, turn, "R-05")
            if words(text).startswith("пока "):
                state = state.model_copy(update={"misunderstandings": 1, "authorization": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Вы придёте на приём? Ответьте, пожалуйста, точно: «да» или «нет».",
                    code="REMINDER_CLARIFICATION",
                )
            return await self.finish(
                state,
                turn,
                "MAYBE",
                "Спасибо за ответ. При необходимости позвоните в регистратуру.",
            )

        # В общем NLU «не смогу прийти» бывает I-03. Здесь это ответ на вопрос
        # о приходе, а не распоряжение отменить запись: сначала уточняем действие.
        if state.phase == Phase.REMIND_DETAILS and text in DECLINE:
            if state.reminder.declined:
                return await self.unknown(state, turn)
            state = state.model_copy(
                update={
                    "reminder": state.reminder.model_copy(update={"declined": True}),
                    "misunderstandings": 0,
                }
            )
            return state, reply(state, turn.turn_id, *self.prompt(state), code="REMINDER_DECLINED")

        result = await self.conversation.input.parse(state, turn)
        if result.call_id != state.call_id or result.turn_id != turn.turn_id:
            raise ValueError("результат NLU не соответствует реплике")
        intents = set(result.intents)
        if {"last_name", "first_name", "middle_name", "birth_date"} & result.slots.model_fields_set:
            return await self.handoff(state, turn, "R-16")
        if "I-01" in intents or "I-04" in intents or mentions_existing_visits(turn.text):
            if "I-02" not in intents and "I-03" not in intents:
                state = state.model_copy(update={"misunderstandings": state.misunderstandings + 1})
                if state.misunderstandings >= 3:
                    return await self.handoff(state, turn, "R-05")
                return state, reply(
                    state,
                    turn.turn_id,
                    "Для другой записи или другого пациента позвоните в регистратуру и пройдите идентификацию.",
                    *self.prompt(state),
                    code="REMINDER_SCOPE",
                )
        if {"I-02", "I-03"} <= intents:
            return await self.unknown(state, turn)
        if "I-02" in intents and state.purpose != "reschedule":
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            return await self._start_change(state, turn, result, cancel=False)
        if "I-03" in intents and state.purpose != "cancel":
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            return await self._start_change(state, turn, result, cancel=True)

        if state.phase == Phase.REMIND_DETAILS:
            if answer == "no" and not state.reminder.declined:
                state = state.model_copy(
                    update={
                        "reminder": state.reminder.model_copy(update={"declined": True}),
                        "misunderstandings": 0,
                    }
                )
                return state, reply(
                    state, turn.turn_id, *self.prompt(state), code="REMINDER_DECLINED"
                )
            return await self.unknown(state, turn)

        if state.phase == Phase.CANCEL_OFFER and answer in {"yes", "no"} and "I-03" not in intents:
            return await self.unknown(state, turn)
        refinements = PREFERENCE_FIELDS & result.slots.model_fields_set or result.slots.doctor
        if answer == "no" and not refinements:
            if await self._pending(state):
                return await self.handoff(state, turn, "R-16")
            return await self._details(state, turn)
        if refinements and "no" in intents:
            result = result.model_copy(
                update={"intents": tuple(i for i in result.intents if i != "no")}
            )
        if state.purpose == "cancel" and self.conversation.cancellation:
            state, response = await self.conversation.cancellation.step(state, turn, result)
        elif state.purpose == "reschedule" and self.conversation.rescheduling:
            if state.phase == Phase.MOVE_OFFERED and len(state.options) == 1 and text in ATTEND_YES:
                result = result.model_copy(
                    update={
                        "intents": ("choice",),
                        "slots": NluSlots(choice_index=1),
                    }
                )
            state, response = await self.conversation.rescheduling.step(state, turn, result)
        else:
            return await self.unknown(state, turn)
        if state.phase == Phase.CANCELLED and response.code == "CANCELLED":
            return await self.finish(
                state,
                turn,
                "CANCELLED",
                "Готово, запись отменена. Спасибо, что предупредили.",
                status="applied",
            )
        if state.phase == Phase.RESCHEDULED and response.code == "RESCHEDULED":
            return await self.finish(
                state,
                turn,
                "RESCHEDULED",
                *response.utterances[:2],
                "Прежняя запись отменена.",
                status="applied",
            )
        return state, response

    async def unknown(self, state, turn):
        state = state.model_copy(
            update={"misunderstandings": state.misunderstandings + 1, "authorization": None}
        )
        if state.misunderstandings >= 3:
            return await self.handoff(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Уточните, пожалуйста, ваш ответ.",
            *self.prompt(state),
            code="NOT_UNDERSTOOD",
        )
