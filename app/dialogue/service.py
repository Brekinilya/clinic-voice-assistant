"""Общий цикл диалога: квитанции, прерывание, safety и сменный сценарий разговора."""

import hashlib
import hmac

from app.booking.models import ActionState, BookingError
from app.booking.service import BookingService
from app.dialogue.failures import mis_failure
from app.dialogue.input import CommandInterpreter, InputInterpreter, Operation
from app.dialogue.models import DialogueState, Phase, Proposal, Reply, TransferRequest, Turn, reply
from app.dialogue.ports import Conversation, DialogueStore, SafetyChecker
from app.dialogue.safety_adapter import route_for
from app.dialogue.transitions import handoff_reply
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
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("ключ квитанций должен содержать хотя бы 32 байта")
        self.booking = booking
        self.store = store
        self._hash_key = hash_key
        self.interpreter = interpreter if interpreter is not None else CommandInterpreter()
        self.safety = safety
        self.conversation = conversation

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
                    await self.booking.store.release_call_holds(call_id)
                    return reply(
                        current,
                        turn.turn_id,
                        "Проверка безопасности недоступна. Автоматическая запись остановлена.",
                        "Подготовлен запрос регистратору.",
                        code="SAFETY_UNAVAILABLE",
                    )
                if transfer is not None:
                    current = await self.store.preempt(call_id, turn.turn_id, transfer)
                    await self.booking.store.release_call_holds(call_id)
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
            state, result = await self._process(state, turn)
        except Exception:
            # Неизвестный сбой закрывает сценарий. Персональные данные исключения не возвращаем.
            await self.booking.store.release_call_holds(call_id)
            state = state.model_copy(
                update={"phase": Phase.INTERRUPTED, "proposal": None, "options": ()}
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
            await self.booking.store.release_call_holds(call_id)
            current = await self.store.inspect(call_id)
            if current.phase == Phase.HANDOFF:
                return handoff_reply(current, turn.turn_id)
            if current.phase != Phase.ENDED:
                return reply(
                    current,
                    turn.turn_id,
                    "Поступила новая реплика. Уточните выбор или подтвердите заново.",
                    code="TURN_INTERRUPTED",
                )
            return reply(current, turn.turn_id, "Разговор завершён.", code="CALL_ENDED")
        return result

    async def end(self, call_id: str) -> Reply:
        state = await self.store.end(call_id)
        # Повтор end снова пытается снять оставшиеся брони, если прежняя попытка прервалась.
        await self.booking.store.release_call_holds(call_id)
        return reply(state, state.last_turn_id, "Разговор завершён.", code="CALL_ENDED")

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
            shortcut = self.conversation.short_reply(state, turn)
            if shortcut is not None:
                return shortcut
        if state.retry_at is not None:
            if state.retry_at > now and (
                command is None or command.operation not in {Operation.STATUS, Operation.HELP}
            ):
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
                options = await self.booking.options(state.client_id, command.specialty)
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
