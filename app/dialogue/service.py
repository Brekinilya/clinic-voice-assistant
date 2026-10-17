"""Общий цикл диалога: квитанции, прерывание, safety и сменный сценарий разговора."""

import hashlib
import hmac
from datetime import timedelta

from app.booking.models import ActionState, BookingError
from app.booking.service import BookingService
from app.dialogue.failures import mis_failure
from app.dialogue.input import CommandInterpreter, InputInterpreter, Operation
from app.dialogue.models import (
    DialogueState,
    InputStart,
    Phase,
    Proposal,
    Reply,
    TransferRequest,
    Turn,
    reply,
)
from app.dialogue.ports import Conversation, DialogueStore, SafetyChecker
from app.dialogue.safety_adapter import route_for
from app.dialogue.transitions import handoff_reply
from app.mis.budget import Priority, demand
from app.mis.models import format_datetime
from app.mis.ports import MisError


class DialogueService:
    def __init__(
        self,
        booking: BookingService,
        store: DialogueStore,
        hash_key: bytes,
        *,
        interpreter: InputInterpreter | None = None,
        safety: SafetyChecker | None = None,
        conversation: Conversation | None = None,
        mis_turn_wait_s: float = 8.0,
        mis_wait_limit_s: int = 120,
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("ключ квитанций должен содержать хотя бы 32 байта")
        self.booking = booking
        self.store = store
        self._hash_key = hash_key
        self.interpreter = interpreter if interpreter is not None else CommandInterpreter()
        self.safety = safety
        self.conversation = conversation
        self.mis_turn_wait_s = mis_turn_wait_s
        self.mis_wait_limit_s = mis_wait_limit_s

    def _digest(self, call_id: str, turn: Turn) -> str:
        # ФИО и дата рождения не сохраняются в журнале команд даже в виде обычного SHA256.
        return hmac.new(
            self._hash_key,
            call_id.encode() + b"\x00" + turn.model_dump_json().encode(),
            hashlib.sha256,
        ).hexdigest()

    async def handle(self, call_id: str, turn: Turn) -> Reply:
        digest = self._digest(call_id, turn)
        if self.safety is not None:
            current = await self.store.observe(call_id, turn.turn_id, digest)
            if current.phase == Phase.ENDED:
                return reply(current, turn.turn_id, "Разговор завершён.", code="CALL_ENDED")
            if current.phase == Phase.HANDOFF:
                return handoff_reply(current, turn.turn_id)
            if turn.turn_id >= current.observed_turn_id:
                try:
                    decision = self.safety.evaluate(call_id, turn)
                    if (
                        decision.call_id != call_id
                        or decision.turn_id != turn.turn_id
                        or decision.final != turn.is_final
                    ):
                        raise ValueError("решение safety не соответствует реплике")
                    transfer = route_for(decision)
                except Exception:
                    # При неисправном проверяющем компоненте запрещаем дальнейшие действия.
                    current = await self.store.preempt(
                        call_id, turn.turn_id, TransferRequest(route="R-08", priority="P3")
                    )
                    await self._release_call(call_id)
                    self.booking.clear_call_cache(call_id)
                    return reply(
                        current,
                        turn.turn_id,
                        "Проверка безопасности недоступна. Автоматическая запись остановлена.",
                        "Подготовлен запрос регистратору.",
                        code="SAFETY_UNAVAILABLE",
                    )
                if transfer is not None:
                    current = await self.store.preempt(call_id, turn.turn_id, transfer)
                    await self._release_call(call_id)
                    self.booking.clear_call_cache(call_id)
                    return handoff_reply(current, turn.turn_id)
        if not turn.is_final:
            state = await self.store.inspect(call_id)
            if state.phase == Phase.ENDED:
                return reply(
                    state, turn.turn_id, "Разговор завершён.", code="CALL_ENDED", kind="partial"
                )
            return reply(state, turn.turn_id, "Ожидаю завершения команды.", kind="partial")
        claim = await self.store.claim(call_id, turn.turn_id, digest)
        if not claim.acquired:
            if self.safety is not None:
                current = await self.store.inspect(call_id)
                if current.phase == Phase.HANDOFF:
                    return handoff_reply(current, turn.turn_id)
            return claim.cached or reply(
                claim.state, turn.turn_id, "Команда ещё обрабатывается.", kind="processing"
            )
        state = claim.state
        try:
            granted = []
            with self._mis_demand(call_id, state, granted.append):
                state, result = await self._process(state, turn)
            state, result = await self._settle_wait(state, turn, result, progressed=bool(granted))
        except Exception:
            # Неизвестный сбой закрывает сценарий. Персональные данные исключения не возвращаем.
            await self._release_call(call_id)
            self.booking.clear_call_cache(call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.INTERRUPTED,
                    "identity_draft": None,
                    "proposal": None,
                    "cancel_proposal": None,
                    "cancel_target": None,
                    "move_proposal": None,
                    "move_target": None,
                    "options": (),
                    "visit_items": (),
                    "visit_cursor": None,
                    "visit_has_more": False,
                }
            )
            result = reply(
                state,
                turn.turn_id,
                "Обработка прервана. Завершите разговор.",
                code="PROCESSING_INTERRUPTED",
            )
        # После аварии процесса или отмены задачи квитанция остаётся processing.
        # Автоматически переисполнять её нельзя; разговор можно явно завершить.
        if not await self.store.complete(state, turn.turn_id, result):
            await self._release_call(call_id)
            current = await self.store.inspect(call_id)
            if current.phase == Phase.HANDOFF:
                self.booking.clear_call_cache(call_id)
                return handoff_reply(current, turn.turn_id)
            if current.phase != Phase.ENDED:
                return reply(
                    current,
                    turn.turn_id,
                    "Поступила новая реплика. Уточните выбор или подтвердите заново.",
                    code="TURN_INTERRUPTED",
                )
            self.booking.clear_call_cache(call_id)
            return reply(current, turn.turn_id, "Разговор завершён.", code="CALL_ENDED")
        if state.phase in {Phase.ENDED, Phase.HANDOFF}:
            self.booking.clear_call_cache(call_id)
        return result

    def _mis_demand(self, call_id: str, state: DialogueState, granted):
        """Ход разговора ждёт окна МИС не дольше срока; новая речь или отбой отменяют ожидание."""
        epoch = state.input_epoch

        async def superseded() -> bool:
            current = await self.store.inspect(call_id)
            return current.input_epoch != epoch or current.phase in {Phase.ENDED, Phase.HANDOFF}

        return demand(
            Priority.DIALOGUE,
            until=self.booking.clock.now() + timedelta(seconds=self.mis_turn_wait_s),
            cancelled=superseded,
            owner=call_id,
            granted=granted,
        )

    async def _settle_wait(
        self, state: DialogueState, turn: Turn, result: Reply, *, progressed: bool
    ) -> tuple[DialogueState, Reply]:
        """Ожидание МИС без движения ограничено: дальше не «подождите», а регистратор.

        Ход, в котором МИС выдала хотя бы одно место (например, перенос создал новую
        запись и ждёт окна для отмены прежней), начинает отсчёт заново.
        """
        if result.code != "WAIT_MIS":
            if state.mis_wait_since is not None or state.retry_at is not None:
                state = state.model_copy(update={"mis_wait_since": None, "retry_at": None})
                result = result.model_copy(update={"retry_at": None})
            return state, result
        now = self.booking.clock.now()
        if state.mis_wait_since is None or progressed:
            state = state.model_copy(update={"mis_wait_since": now})
        deadline = state.mis_wait_since + timedelta(seconds=self.mis_wait_limit_s)
        if self.conversation is not None and (state.retry_at or now) > deadline:
            state = await self.store.preempt(
                state.call_id,
                turn.turn_id,
                TransferRequest(route="R-06", priority="P3", reason="mis_wait"),
            )
            await self._release_call(state.call_id)
            return state, handoff_reply(state, turn.turn_id)
        return state, result.model_copy(update={"wait_deadline": deadline})

    async def input_started(self, call_id: str, turn_id: int) -> Reply:
        event = InputStart(turn_id=turn_id)
        state = await self.store.start_input(call_id, event.turn_id)
        if state.phase == Phase.HANDOFF:
            return handoff_reply(state, event.turn_id)
        return reply(
            state,
            event.turn_id,
            code="CALL_ENDED" if state.phase == Phase.ENDED else "INPUT_STARTED",
            kind="partial",
        )

    async def partial(self, call_id: str, turn: Turn) -> Reply:
        """Шлюз вызывает вне очереди итоговых реплик; бизнес-действий здесь нет."""
        if turn.is_final:
            raise ValueError("для partial нужен is_final=False")
        return await self.handle(call_id, turn)

    async def end(self, call_id: str) -> Reply:
        state = await self.store.end(call_id)
        # Повтор end снова пытается снять оставшиеся брони, если прежняя попытка прервалась.
        await self._release_call(call_id)
        self.booking.clear_call_cache(call_id)
        return reply(state, state.last_turn_id, "Разговор завершён.", code="CALL_ENDED")

    async def _release_call(self, call_id: str) -> None:
        if self.conversation is not None:
            await self.conversation.release_call(call_id)
        else:
            await self.booking.store.release_call_holds(call_id)

    async def _process(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        command = (
            None if self.conversation is not None else await self.interpreter.interpret(state, turn)
        )
        now = self.booking.clock.now()
        if state.phase == Phase.INTERRUPTED:
            return state, reply(
                state,
                turn.turn_id,
                "Обработка прервана. Завершите разговор.",
                code="PROCESSING_INTERRUPTED",
            )
        if state.phase == Phase.HELD and state.proposal is not None:
            action = await self.booking.store.get(state.proposal.action_id)
            if action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }:
                state = state.model_copy(update={"phase": Phase.RECONCILING})
            elif state.proposal.expires_at <= now:
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={"phase": Phase.IDENTIFIED, "proposal": None, "options": ()}
                )
        if self.conversation is not None:
            shortcut = await self.conversation.short_reply(state, turn)
            if shortcut is not None:
                return shortcut
        # В разговоре реплика понимается и во время паузы: «нет», «оператора» и новые
        # предпочтения не требуют МИС, а запросы к ней сами упрутся в общий бюджет.
        if self.conversation is None and state.retry_at is not None:
            if state.retry_at > now and command.operation not in {
                Operation.STATUS,
                Operation.HELP,
            }:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Лимит запросов исчерпан. Повторите просьбу после указанного времени.",
                    code="WAIT_MIS",
                )
            if state.retry_at <= now:
                state = state.model_copy(update={"retry_at": None})
        try:
            if self.conversation is not None:
                return await self.conversation.step(state, turn)
            if command.operation == Operation.HELP:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Это автономный командный стенд с вымышленными данными.",
                    "Укажите: пациент Фамилия; Имя; Отчество; ДД.ММ.ГГГГ.",
                    "Затем: варианты терапевт, выбрать 1, нет или статус.",
                )
            if command.operation == Operation.STATUS:
                return state, reply(state, turn.turn_id, "Текущее состояние показано в ответе.")
            if command.operation == Operation.IDENTIFY:
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.READY,
                        "client_id": None,
                        "options": (),
                        "proposal": None,
                    }
                )
                if command.patient is None:
                    raise ValueError("формат команды")
                data = command.patient
                patient = await self.booking.identify(
                    data.last_name, data.first_name, data.middle_name, data.birth_date
                )
                state = state.model_copy(
                    update={"phase": Phase.IDENTIFIED, "client_id": patient.id}
                )
                return state, reply(
                    state, turn.turn_id, "Пациент найден. Укажите специальность: варианты терапевт."
                )
            if (
                command.operation == Operation.OPTIONS
                and command.specialty is not None
                and state.client_id is not None
            ):
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={"phase": Phase.IDENTIFIED, "proposal": None, "options": ()}
                )
                options = await self.booking.options(
                    state.client_id, command.specialty, call_id=state.call_id
                )
                state = state.model_copy(
                    update={
                        "phase": Phase.OFFERED if options else Phase.IDENTIFIED,
                        "options": options,
                    }
                )
                phrases = tuple(
                    f"Вариант {i}: {format_datetime(slot.scheduled_at)}, {slot.specialty}."
                    for i, slot in enumerate(options, 1)
                )
                return state, reply(
                    state, turn.turn_id, *(phrases or ("Доступных подтверждённых вариантов нет.",))
                )
            if command.operation == Operation.SELECT and state.client_id is not None:
                index = command.choice_index
                if index is None or not 1 <= index <= len(state.options):
                    raise ValueError("номер варианта")
                action = await self.booking.propose(
                    state.call_id, state.client_id, state.options[index - 1]
                )
                proposal = Proposal(
                    action_id=action.action_id,
                    revision=action.revision,
                    slot=action.slot,
                    expires_at=action.expires_at,
                )
                state = state.model_copy(update={"phase": Phase.HELD, "proposal": proposal})
                doctors = {d.id: d for d in await self.booking.mis.doctors(action.slot.specialty)}
                doctor = doctors[action.slot.doctor_id]
                return state, reply(
                    state,
                    turn.turn_id,
                    f"Бронь: {format_datetime(action.slot.scheduled_at)}, {doctor.full_name}, {doctor.specialty}.",
                    "Создание записи через этот API пока закрыто до подключения safety.",
                    code="AWAITING_INTEGRATION",
                )
            if command.operation == Operation.DECLINE:
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.IDENTIFIED if state.client_id else Phase.READY,
                        "proposal": None,
                        "options": (),
                    }
                )
                return state, reply(
                    state, turn.turn_id, "Местная бронь снята. Можно запросить другие варианты."
                )
            if command.operation == Operation.CONFIRM:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Создание записи через этот API пока закрыто до подключения safety.",
                    code="WRITES_DISABLED",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Команда не распознана. Используйте помощь.",
                code="UNKNOWN_COMMAND",
            )
        except MisError as error:
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError:
            return state, reply(
                state,
                turn.turn_id,
                "Действие недоступно. Уточните данные или запросите новые варианты.",
                code="BOOKING_REJECTED",
            )
        except (ValueError, TypeError):
            return state, reply(
                state,
                turn.turn_id,
                "Проверьте формат команды и номер варианта.",
                code="INVALID_COMMAND",
            )
