"""Общий цикл диалога: квитанции, прерывание, safety и сменный сценарий разговора."""

import hashlib
import hmac
from datetime import timedelta

from app.booking.models import ActionState, BookingError
from app.booking.service import BookingService
from app.diagnostics.recorder import emit, observe, opt_out
from app.dialogue.failures import mis_failure
from app.dialogue.input import CommandInterpreter, InputInterpreter, Operation
from app.dialogue.models import (
    DialogueError,
    DialogueState,
    InputStart,
    Phase,
    Proposal,
    Reply,
    TransferRequest,
    Turn,
    reply,
)
from app.dialogue.outbound import (
    OutboundContext,
    OutboundEvent,
    ReminderActions,
    ReminderBinding,
    ReminderSession,
)
from app.dialogue.ports import Conversation, DialogueStore, SafetyChecker
from app.dialogue.reminder import ReminderConversation
from app.dialogue.safety_adapter import route_for
from app.dialogue.transitions import handoff_reply
from app.mis.budget import Priority, demand
from app.mis.models import format_datetime
from app.mis.ports import MisError
from app.nlu.parsers.answers import parse_answer


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
        reminder_actions: ReminderActions | None = None,
        handoffs=None,
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("ключ квитанций должен содержать хотя бы 32 байта")
        self.booking = booking
        self.store = store
        self._hash_key = hash_key
        self.interpreter = interpreter if interpreter is not None else CommandInterpreter()
        self.safety = safety
        self.conversation = conversation
        self.handoffs = handoffs
        self.mis_turn_wait_s = mis_turn_wait_s
        self.mis_wait_limit_s = mis_wait_limit_s
        self.debug = None
        if reminder_actions is not None and conversation is None:
            raise ValueError("исходящему диалогу нужен естественный сценарий")
        self.reminders = (
            ReminderConversation(conversation, reminder_actions)
            if reminder_actions is not None
            else None
        )

    def _outbound_digest(self, call_id: str, kind: str, payload: str) -> str:
        return hmac.new(
            self._hash_key, f"{call_id}\0outbound:{kind}\0{payload}".encode(), hashlib.sha256
        ).hexdigest()

    async def open_outbound(self, call_id: str, context: OutboundContext) -> Reply:
        """Внутренний порт исполнителя. Публичный HTTP не принимает этот контекст."""
        if self.reminders is None:
            raise DialogueError("OUTBOUND_NOT_CONFIGURED")
        context = OutboundContext.model_validate(context)
        digest = self._outbound_digest(call_id, "open", context.model_dump_json())
        claim = await self.store.claim(call_id, 1, digest)
        if not claim.acquired:
            return claim.cached or reply(claim.state, 1, kind="processing")
        state = claim.state.model_copy(
            update={
                "phase": Phase.REMIND_VERIFY,
                "reminder": ReminderSession(
                    binding=ReminderBinding.model_validate(
                        context.model_dump(exclude={"addressee"})
                    ),
                    context_digest=digest,
                ),
                "client_id": None,
            }
        )
        try:
            await self.reminders.actions.check_attempt(state.reminder.binding)
            if context.visit.scheduled_at <= self.booking.clock.now():
                raise BookingError("VISIT_PAST")
            result = reply(
                state,
                1,
                "Здравствуйте! Я виртуальный помощник регистратуры.",
                f"Это {context.addressee}?",
                code="REMINDER_VERIFY",
            )
        except Exception:
            # После сбоя открытия этот call_id нельзя переиспользовать для нового звонка.
            state, result = await self.reminders.finish(
                state,
                Turn(turn_id=1, text="открытие"),
                "ESCALATED",
                "Не удалось начать напоминание. Обратитесь в регистратуру.",
            )
        return await self._complete_outbound(state, 1, result)

    async def outbound_event(self, call_id: str, event: OutboundEvent) -> Reply:
        if self.reminders is None:
            raise DialogueError("OUTBOUND_NOT_CONFIGURED")
        event = OutboundEvent.model_validate(event)
        current = await self.store.inspect(call_id)
        if current.reminder is None or current.reminder.binding.attempt_id != event.attempt_id:
            raise DialogueError("OUTBOUND_ATTEMPT_CONFLICT")
        digest = self._outbound_digest(call_id, "event", event.model_dump_json())
        await self.store.observe(call_id, event.turn_id, digest)
        claim = await self.store.claim(call_id, event.turn_id, digest)
        if not claim.acquired:
            return claim.cached or reply(claim.state, event.turn_id, kind="processing")
        turn = Turn(turn_id=event.turn_id, text="автоответчик")
        state, result = await self.reminders.finish(
            claim.state, turn, "VOICEMAIL", "Пожалуйста, перезвоните в регистратуру. Всего доброго."
        )
        return await self._complete_outbound(state, event.turn_id, result)

    async def _complete_outbound(self, state, turn_id, result):
        if not await self.store.complete(state, turn_id, result):
            await self._release_call(state.call_id)
            state = await self.store.inspect(state.call_id)
            if state.phase == Phase.HANDOFF:
                return handoff_reply(state, turn_id)
            return reply(state, turn_id, "Разговор прерван.", code="TURN_INTERRUPTED")
        if self.debug is not None:
            # Имя адресата и сырой OutboundContext не передаются в диагностику открытия.
            self.debug.emit(
                state.call_id, turn_id, "dialogue.outbound", phase=state.phase, code=result.code
            )
        return result

    def _digest(self, call_id: str, turn: Turn) -> str:
        # ФИО и дата рождения не сохраняются в журнале команд даже в виде обычного SHA256.
        return hmac.new(
            self._hash_key,
            call_id.encode() + b"\x00" + turn.model_dump_json().encode(),
            hashlib.sha256,
        ).hexdigest()

    async def handle(self, call_id: str, turn: Turn, *, source: str = "phone") -> Reply:
        with observe(self.debug, call_id, turn.turn_id):
            emit("dialogue.input", text=turn.text, final=turn.is_final)
            result = await self._handle(call_id, turn, source=source)
            if self.debug is not None:
                emit(
                    "dialogue.reply",
                    **result.model_dump(mode="json", exclude={"call_id", "turn_id"}),
                )
            return result

    async def _handle(self, call_id: str, turn: Turn, *, source: str = "phone") -> Reply:
        digest = self._digest(call_id, turn)
        if self.safety is not None:
            current = await self.store.observe(call_id, turn.turn_id, digest)
            if current.phase == Phase.ENDED:
                if turn.is_final and turn.turn_id <= current.last_turn_id:
                    try:
                        claim = await self.store.claim(call_id, turn.turn_id, digest)
                    except DialogueError as error:
                        if str(error) != "CALL_ENDED":
                            raise
                    else:
                        if claim.cached is not None:
                            return claim.cached
                return reply(current, turn.turn_id, "Разговор завершён.", code="CALL_ENDED")
            if current.phase == Phase.HANDOFF:
                if self.handoffs is not None:
                    if turn.is_final:
                        await self.handoffs.callback_final_observed(call_id, turn.turn_id)
                    else:
                        # Промежуточная речь отзывает согласие даже без отдельного
                        # input_started. Итоговый HTTP-ввод сохраняет строгий порядок.
                        await self.handoffs.callback_input_started(call_id, turn.turn_id)
                if self.handoffs is None or not await self.handoffs.has_callback_draft(call_id):
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
                    if "no_recording" in decision.flags:
                        opt_out()
                    if self.debug is not None:
                        emit(
                            "safety.checked",
                            **decision.model_dump(mode="json", exclude={"call_id", "turn_id"}),
                        )
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
                    if self.handoffs is not None:
                        if source == "text" and self.handoffs.settings.enabled:
                            await self.handoffs.create_text_request(
                                transfer.route,
                                transfer.priority,
                                "Текстовый запрос сотруднику",
                                call_id=call_id,
                            )
                        await self.handoffs.discard_draft(call_id)
                    current = await self.store.preempt(call_id, turn.turn_id, transfer)
                    await self._release_call(call_id)
                    self.booking.clear_call_cache(call_id)
                    return handoff_reply(current, turn.turn_id)
            if current.phase == Phase.HANDOFF:
                callback_result = await self.handoffs.callback_turn(call_id, turn)
                if callback_result.phase == Phase.ENDED:
                    state, saved_reply = await self.store.finish_handoff_turn(
                        call_id, turn.turn_id, digest, current.version, callback_result
                    )
                    if saved_reply is not None:
                        return saved_reply
                    if state.phase == Phase.HANDOFF:
                        return handoff_reply(state, turn.turn_id)
                    return reply(state, turn.turn_id, "Разговор завершён.", code="CALL_ENDED")
                return callback_result
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
        except Exception as error:
            emit("dialogue.error", error=type(error).__name__)
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
        # HandoffStore serializes this epoch bump with callback-task creation.
        # Whichever transaction commits first defines whether consent was current.
        if self.handoffs is not None:
            await self.handoffs.callback_input_started(call_id, event.turn_id)
        state = await self.store.start_input(call_id, event.turn_id)
        if self.debug is not None:
            self.debug.emit(
                call_id, turn_id, "dialogue.input_started", input_epoch=state.input_epoch
            )
        if state.phase == Phase.HANDOFF:
            if self.handoffs is not None:
                if await self.handoffs.has_callback_draft(call_id):
                    return reply(
                        state, event.turn_id, code="CALLBACK_INPUT_STARTED", kind="partial"
                    )
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
        if self.handoffs is not None:
            await self.handoffs.discard_draft(call_id)
        state = await self.store.end(call_id)
        # Повтор end снова пытается снять оставшиеся брони, если прежняя попытка прервалась.
        await self._release_call(call_id)
        self.booking.clear_call_cache(call_id)
        if self.debug is not None:
            self.debug.emit(call_id, None, "dialogue.ended", phase=state.phase)
        return reply(state, state.last_turn_id, "Разговор завершён.", code="CALL_ENDED")

    async def start_callback(self, call_id: str, turn_id: int, outcome: str):
        if self.handoffs is None:
            return None
        return await self.handoffs.start_callback(call_id, turn_id, outcome)

    async def _release_call(self, call_id: str) -> None:
        if self.conversation is not None:
            await self.conversation.release_call(call_id)
        else:
            await self.booking.store.release_call_holds(call_id)

    async def _process(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        emit(
            "dialogue.state",
            phase=state.phase,
            purpose=state.purpose,
            input_epoch=state.input_epoch,
            version=state.version,
            misunderstandings=state.misunderstandings,
        )
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
        if state.end_confirmation_pending:
            answer = parse_answer(turn.text)
            if answer == "yes":
                await self._release_call(state.call_id)
                ended = await self.store.end(state.call_id)
                return ended, reply(
                    ended, turn.turn_id, "Спасибо за обращение. До свидания.", code="CALL_ENDED"
                )
            if answer == "no":
                state = state.model_copy(update={"end_confirmation_pending": False})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Хорошо, продолжаем. Ответьте на текущий вопрос: да или нет.",
                    code="END_CANCELLED",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Завершить разговор без выполнения действия? Ответьте «да» или «нет».",
                code="END_CONFIRMATION_REQUIRED",
            )
        contextual_handler = getattr(self.conversation, "cross_scenario_reply", None)
        if state.reminder is not None:
            # Outbound reminder phases own the current question, including the
            # cancellation offer. Let ReminderConversation classify each turn.
            if self.reminders is None:
                raise DialogueError("OUTBOUND_NOT_CONFIGURED")
            return await self.reminders.step(state, turn)
        if contextual_handler is not None:
            contextual = await contextual_handler(state, turn)
            if contextual is not None:
                return contextual
        if state.phase == Phase.HELD and state.proposal is not None:
            action = await self.booking.store.get(state.proposal.action_id)
            if action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }:
                state = state.model_copy(update={"phase": Phase.RECONCILING})
            elif state.proposal.expires_at <= now or action.state == ActionState.EXPIRED:
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.IDENTIFIED,
                        "proposal": None,
                        "options": (),
                        "authorization": None,
                    }
                )
                # Позднее согласие относится к старому предложению. Объясняем
                # причину без МИС; новую дату обрабатывает обычный сценарий.
                if parse_answer(turn.text) == "yes":
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Срок подтверждения выбранного времени истёк.",
                        "Назовите подходящий день или время, и я предложу новые варианты.",
                        code="PROPOSAL_EXPIRED",
                    )
        if contextual_handler is not None:
            contextual = await contextual_handler(state, turn)
            if contextual is not None:
                return contextual
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
