"""Оркестрация локальной карточки, достоверных исходов и ручного callback."""

import asyncio
import base64
import hashlib
import re
from datetime import datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.booking.models import BookingModel
from app.clock import CLINIC_TIMEZONE
from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.handoffs.calendar import CalendarUnavailable, callback_deadline
from app.handoffs.models import (
    CallbackAttempt,
    CallbackDraft,
    CallbackState,
    CallbackTask,
    HandoffRequest,
    MisDeliveryState,
    MisOperation,
    TransferAttempt,
    TransferState,
)
from app.handoffs.ports import HandoffConflict
from app.mis.budget import Priority, demand
from app.mis.models import EscalationCreate
from app.mis.ports import BudgetExhausted, MisError, MisOutcomeUnknown, MisUnavailable
from app.nlu.parsers.answers import parse_answer
from app.nlu.parsers.numbers import parse_digits


class HandoffSettings(BookingModel):
    enabled: bool = False
    callback_enabled: bool = False
    callback_endpoint: str | None = None
    technical_client_id: int | None = None
    worker_poll_s: float = 1.0


class HandoffService:
    def __init__(self, store, mis, clock, hash_key: bytes, settings: HandoffSettings):
        self.store, self.mis, self.clock, self.settings = store, mis, clock, settings
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(b"t08-callback\0" + hash_key).digest())
        )

    async def ensure_request(
        self, call_id: str, route: str, priority: str, state: DialogueState | None = None,
        *, source: str = "phone", summary_override: str | None = None
    ) -> HandoffRequest:
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        known = state is not None and state.client_id is not None
        purpose = state.purpose if state is not None else "unknown"
        specialty = state.specialty if state is not None else None
        summary = "Запрос сотрудника"
        if purpose == "booking":
            summary = "Вопрос по записи"
        elif purpose == "cancel":
            summary = "Вопрос по отмене приёма"
        elif purpose == "reschedule":
            summary = "Вопрос по переносу приёма"
        elif purpose == "visits":
            summary = "Вопрос о существующей записи"
        if specialty:
            summary = f"{summary}; специальность: {specialty}"
        summary = f"{summary}. Причина маршрута: {route}."[:200]
        if summary_override is not None:
            summary = re.sub(r"\s+", " ", summary_override).strip()[:200]
        request = HandoffRequest(
            request_id=uuid4().hex,
            call_id=call_id,
            source=source,
            created_at=now,
            route=route,
            priority=priority,
            identity_status="verified" if known else "unknown",
            client_id=state.client_id if known else None,
            purpose=purpose,
            summary=summary,
            recording_opt_out=state.recording_opt_out if state is not None else None,
            mis_state=MisDeliveryState.MANUAL if priority == "P1" else MisDeliveryState.NOT_REQUIRED,
        )
        saved = await self.store.create_request(request)
        return saved

    async def create_text_request(self, route: str, priority: str, summary: str) -> HandoffRequest:
        """Создаёт карточку текста без телефонной попытки и без подтверждения связи."""
        clean = re.sub(r"[\x00-\x1f\x7f]+", " ", summary)
        return await self.ensure_request(
            uuid4().hex, route, priority, None, source="text", summary_override=clean
        )

    async def transfer_started(self, event, dialogue_state=None) -> None:
        """Вызывается перед originate; только локальная транзакция, без запроса к МИС."""
        request = await self.ensure_request(
            event.call_id, event.route or "R-08", event.priority, dialogue_state
        )
        await self.store.begin_attempt(
            TransferAttempt(
                attempt_id=event.attempt_id,
                request_id=request.request_id,
                call_id=event.call_id,
                route=event.route,
                priority=event.priority,
                started_at=self.clock.now().astimezone(CLINIC_TIMEZONE),
            )
        )

    async def transfer_result(self, event) -> None:
        request = await self.store.request_for_call(event.call_id)
        if request is None or request.attempt_id != event.attempt_id:
            return
        outcome = TransferState(event.outcome)
        operation = None
        if request.mis_operation_id is None and request.priority == "P1" and self.settings.technical_client_id is not None:
            operation = self._new_mis_operation(
                request, "emergency_notice", self.settings.technical_client_id, outcome
            )
        elif (request.mis_operation_id is None and outcome == TransferState.CONNECTED
              and request.identity_status == "verified"
              and request.client_id is not None):
            operation = self._new_mis_operation(request, "transfer", request.client_id, outcome)
        try:
            saved, _ = await self.store.finish_attempt(
                event.attempt_id,
                outcome,
                self.clock.now().astimezone(CLINIC_TIMEZONE),
                event.reason,
                call_id=event.call_id,
                route=event.route,
                priority=event.priority,
                operation=operation,
            )
        except HandoffConflict:
            # Повтор/конфликт сохраняется для ручной сверки, но не блокирует телефон.
            return
        if saved.attempt_id != event.attempt_id:
            return
        if (operation is None and saved.mis_operation_id is None
                and (request.priority == "P1" or outcome == TransferState.CONNECTED)):
            await self._mark_request(saved, mis_state=MisDeliveryState.MANUAL)

    def _new_mis_operation(
        self, request: HandoffRequest, kind: str, client_id: int, outcome: TransferState
    ) -> MisOperation:
        operation_id = uuid4().hex
        marker = f"T08:{operation_id}"
        label = "Экстренное уведомление" if kind == "emergency_notice" else "Передача разговора"
        description = f"[{marker}] {label}. Маршрут {request.route}, исход {outcome.value}. {request.summary}"[:1000]
        return MisOperation(
            operation_id=operation_id,
            request_id=request.request_id,
            kind=kind,
            client_id=client_id,
            description=description,
            marker=marker,
            state=MisDeliveryState.PENDING,
            created_at=self.clock.now().astimezone(CLINIC_TIMEZONE),
        )

    async def _prepare_operation(self, request: HandoffRequest, kind: str, client_id: int) -> None:
        current = await self.store.get_request(request.request_id)
        if current is None or current.mis_operation_id:
            return
        operation = self._new_mis_operation(current, kind, client_id, current.transfer_state)
        try:
            saved = await self.store.create_mis_operation(operation)
        except HandoffConflict:
            return
        current = await self.store.get_request(request.request_id)
        if current is not None:
            await self._mark_request(current, mis_state=MisDeliveryState.PENDING,
                                     mis_operation_id=saved.operation_id)

    async def _mark_request(self, request, **changes):
        try:
            return await self.store.save_request(
                request.model_copy(update=changes), expected_version=request.version
            )
        except HandoffConflict:
            return await self.store.get_request(request.request_id)

    async def start_callback(self, call_id: str, turn_id: int, outcome: str) -> Reply | None:
        if not self.settings.enabled or not self.settings.callback_enabled:
            return None
        request = await self.store.request_for_call(call_id)
        if (
            request is None
            or request.priority == "P1"
            or request.transfer_state not in {TransferState.NO_ANSWER, TransferState.FAILED}
        ):
            return None
        existing = await self.store.get_callback_task_by_request(request.request_id)
        if existing is not None:
            return self._callback_reply(
                call_id, turn_id,
                ("Задача обратного звонка уже сохранена. Всего доброго.",),
                code="CALLBACK_QUEUED", ended=True,
            )
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        try:
            deadline = callback_deadline(now, request.priority)
        except CalendarUnavailable:
            await self._mark_request(request, callback_state=CallbackState.MANUAL)
            return self._callback_reply(call_id, turn_id,
                ("Сейчас не могу проверить срок обратного звонка. Обращение доступно сотруднику для ручной обработки.",),
                code="CALLBACK_CALENDAR_UNAVAILABLE", ended=True)
        existing_draft = await self.store.get_callback_draft(request.request_id)
        if existing_draft is not None:
            return self._callback_prompt(call_id, turn_id, existing_draft)
        draft = CallbackDraft(
            request_id=request.request_id,
            stage="offer",
            deadline=deadline,
            summary=request.summary,
            last_turn_id=turn_id,
        )
        try:
            await self.store.save_callback_draft(draft, expected_version=None)
        except HandoffConflict:
            existing_draft = await self.store.get_callback_draft(request.request_id)
            if existing_draft is None:
                return None
            return self._callback_prompt(call_id, turn_id, existing_draft)
        request = await self.store.get_request(request.request_id)
        await self._mark_request(request, callback_state=CallbackState.DRAFT,
                                 callback_deadline=deadline)
        return self._callback_reply(
            call_id, turn_id,
            ("Регистратор не ответил.", "Могу сохранить задачу для обратного звонка.",
             "Согласны?"),
            code="CALLBACK_OFFERED",
        )

    async def callback_input_started(self, call_id: str, turn_id: int) -> None:
        await self.store.advance_callback_input(call_id, turn_id)

    async def has_callback_draft(self, call_id: str) -> bool:
        request = await self.store.request_for_call(call_id)
        return bool(request and await self.store.get_callback_draft(request.request_id))

    async def callback_turn(self, call_id: str, turn: Turn) -> Reply:
        request = await self.store.request_for_call(call_id)
        if request is None:
            return self._callback_reply(call_id, turn.turn_id,
                ("Запрос уже завершён.",), code="CALLBACK_UNAVAILABLE", ended=True)
        task = await self.store.get_callback_task_by_request(request.request_id)
        if task is not None:
            if turn.turn_id < task.consent_turn_id:
                return self._callback_reply(call_id, turn.turn_id,
                    ("Ответ устарел; задача уже сохранена по предыдущему подтверждению.",),
                    code="CALLBACK_STALE")
            return self._callback_reply(call_id, turn.turn_id,
                ("Задача обратного звонка сохранена.", "Всего доброго."),
                code="CALLBACK_QUEUED", ended=True)
        draft = await self.store.get_callback_draft(request.request_id)
        if draft is None:
            return self._callback_reply(call_id, turn.turn_id,
                ("Обратный звонок не оформлен.",), code="CALLBACK_NOT_QUEUED", ended=True)
        if turn.turn_id <= draft.last_turn_id:
            return self._callback_prompt(call_id, turn.turn_id, draft)
        if turn.turn_id != draft.last_turn_id + 1:
            return self._callback_reply(call_id, turn.turn_id,
                ("Реплика устарела. Повторите ответ после сигнала.",), code="CALLBACK_STALE")
        if not turn.is_final:
            return self._callback_reply(call_id, turn.turn_id,
                ("Ожидаю, когда вы закончите.",), code="CALLBACK_PARTIAL")
        text = turn.text.strip()
        answer = parse_answer(text)
        if draft.stage == "offer":
            if answer == "no":
                await self.store.delete_callback_draft(
                    request.request_id, expected_version=draft.version
                )
                request = await self.store.get_request(request.request_id)
                await self._mark_request(request, callback_state=CallbackState.CANCELLED)
                return self._callback_reply(call_id, turn.turn_id,
                    ("Хорошо. Обратный звонок не оформлен.", "Всего доброго."),
                    code="CALLBACK_DECLINED", ended=True)
            if answer != "yes":
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Сохранить задачу для обратного звонка? Ответьте да или нет.",))
            draft = draft.model_copy(update={"stage": "contact", "last_turn_id": turn.turn_id,
                                             "invalid_turns": 0,
                                             "version": draft.version + 1})
            await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
            return self._callback_prompt(call_id, turn.turn_id, draft,
                ("Назовите номер телефона для обратного звонка, начиная с кода страны.",))
        if draft.stage == "contact":
            digits = parse_digits(text, 11) or parse_digits(text, 10)
            raw_digits = re.sub(r"\D", "", text)
            digits = digits or (raw_digits if len(raw_digits) in {10, 11} else None)
            if digits is None or len(digits) not in {10, 11}:
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Не удалось проверить номер.", "Продиктуйте 10 или 11 цифр ещё раз."))
            if len(digits) == 10:
                digits = "7" + digits
            elif digits.startswith("8"):
                digits = "7" + digits[1:]
            if len(digits) != 11 or not digits.startswith("7"):
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Нужен российский номер с кодом 7 или 8. Назовите номер ещё раз.",))
            contact = "+" + digits
            draft = draft.model_copy(update={
                "stage": "time", "contact_ciphertext": self.cipher.encrypt(contact.encode()).decode(),
                "contact_masked": "•••• " + digits[-4:],
                "last_turn_id": turn.turn_id, "invalid_turns": 0,
                "version": draft.version + 1,
            })
            await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
            return self._callback_prompt(call_id, turn.turn_id, draft,
                (f"Я записала номер, оканчивающийся на {digits[-4:]}.",
                 "Есть удобное время для звонка? Если нет, скажите «в ближайшее время».",))
        if draft.stage == "time":
            if answer == "no" or "ближайш" in text.casefold():
                draft = draft.model_copy(update={"stage": "confirm", "last_turn_id": turn.turn_id,
                                                 "invalid_turns": 0,
                                                 "version": draft.version + 1})
                await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
                return self._callback_prompt(call_id, turn.turn_id, draft,
                    self._confirmation_lines(request, draft))
            if answer == "repeat" or "когда" in text.casefold():
                try:
                    fresh = callback_deadline(
                        self.clock.now().astimezone(CLINIC_TIMEZONE), request.priority
                    )
                except CalendarUnavailable:
                    fresh = draft.deadline
                if fresh > draft.deadline + timedelta(minutes=5):
                    updated = draft.model_copy(update={
                        "deadline": fresh, "last_turn_id": turn.turn_id,
                        "version": draft.version + 1,
                    })
                    await self.store.save_callback_draft(updated, expected_version=draft.version)
                    draft = updated
                return self._callback_prompt(call_id, turn.turn_id, draft,
                    (f"Нормативный срок — до {format_clinic(draft.deadline)}.",
                     "Есть удобное время?"))
            preferred = parse_preferred_time(text, self.clock.now().astimezone(CLINIC_TIMEZONE))
            if preferred is None:
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Не удалось определить дату и время.",
                     "Скажите «в ближайшее время» или назовите дату и время."))
            draft = draft.model_copy(update={"stage": "confirm", "preferred_at": preferred,
                                             "last_turn_id": turn.turn_id,
                                             "invalid_turns": 0,
                                             "version": draft.version + 1})
            await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
            lines = self._confirmation_lines(request, draft)
            if preferred > draft.deadline:
                lines = (f"Вы назвали время позже норматива {format_clinic(draft.deadline)}.",
                         f"Подтвердите ожидание до {format_clinic(preferred)}.", *lines)
            return self._callback_prompt(call_id, turn.turn_id, draft, lines)
        if draft.stage == "goal":
            goals = {
                "запись": "Вопрос по записи",
                "отмена": "Вопрос по отмене приёма",
                "перенос": "Вопрос по переносу приёма",
                "другое": "Другой вопрос регистратуре",
            }
            selected = next((label for key, label in goals.items() if key in text.casefold()), None)
            if selected is None:
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Выберите цель: запись, отмена, перенос или другой вопрос.",))
            draft = draft.model_copy(update={
                "stage": "confirm", "summary": selected,
                "last_turn_id": turn.turn_id, "invalid_turns": 0,
                "version": draft.version + 1,
            })
            await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
            return self._callback_prompt(call_id, turn.turn_id, draft,
                self._confirmation_lines(request, draft))
        if draft.stage == "confirm":
            if answer == "repeat" or "когда" in text.casefold():
                try:
                    fresh = callback_deadline(
                        self.clock.now().astimezone(CLINIC_TIMEZONE), request.priority
                    )
                except CalendarUnavailable:
                    fresh = draft.deadline
                if fresh > draft.deadline + timedelta(minutes=5):
                    draft = draft.model_copy(update={
                        "deadline": fresh, "last_turn_id": turn.turn_id,
                        "version": draft.version + 1,
                    })
                    await self.store.save_callback_draft(draft, expected_version=draft.version - 1)
                return self._callback_prompt(call_id, turn.turn_id, draft,
                    self._confirmation_lines(request, draft))
            lowered = text.casefold()
            if any(phrase in lowered for phrase in ("изменить цель", "другая тема", "другой вопрос")):
                updated = draft.model_copy(update={
                    "stage": "goal", "last_turn_id": turn.turn_id,
                    "invalid_turns": 0, "version": draft.version + 1,
                })
                await self.store.save_callback_draft(updated, expected_version=draft.version)
                return self._callback_prompt(call_id, turn.turn_id, updated,
                    ("Уточните новую цель: запись, отмена, перенос или другой вопрос.",))
            digits = parse_digits(text, 11) or parse_digits(text, 10)
            raw_digits = re.sub(r"\D", "", text)
            digits = digits or (raw_digits if len(raw_digits) in {10, 11} else None)
            if digits is not None:
                if len(digits) == 10:
                    digits = "7" + digits
                elif len(digits) == 11 and digits.startswith("8"):
                    digits = "7" + digits[1:]
                if len(digits) != 11 or not digits.startswith("7"):
                    return await self._retry_callback_input(call_id, turn, request, draft,
                        ("Нужен российский номер с кодом 7 или 8. Назовите его ещё раз.",))
                updated = draft.model_copy(update={
                    "contact_ciphertext": self.cipher.encrypt(("+" + digits).encode()).decode(),
                    "contact_masked": "•••• " + digits[-4:],
                    "last_turn_id": turn.turn_id, "invalid_turns": 0,
                    "version": draft.version + 1,
                })
                await self.store.save_callback_draft(updated, expected_version=draft.version)
                return self._callback_prompt(call_id, turn.turn_id, updated,
                    (f"Номер исправлен, оканчивается на {digits[-4:]}.",
                     *self._confirmation_lines(request, updated)))
            if answer != "yes":
                if answer == "no":
                    await self.store.delete_callback_draft(
                        request.request_id, expected_version=draft.version
                    )
                    current = await self.store.get_request(request.request_id)
                    if current is not None:
                        await self._mark_request(current, callback_state=CallbackState.CANCELLED)
                    return self._callback_reply(call_id, turn.turn_id,
                        ("Не сохраняю задачу обратного звонка.", "Всего доброго."),
                        code="CALLBACK_DECLINED", ended=True)
                return await self._retry_callback_input(call_id, turn, request, draft,
                    ("Подтвердите сохранение задачи обратного звонка: да или нет.",))
            now = self.clock.now().astimezone(CLINIC_TIMEZONE)
            try:
                deadline = callback_deadline(now, request.priority)
            except CalendarUnavailable:
                await self.store.delete_callback_draft(
                    request.request_id, expected_version=draft.version
                )
                current = await self.store.get_request(request.request_id)
                if current is not None:
                    await self._mark_request(current, callback_state=CallbackState.MANUAL)
                return self._callback_reply(call_id, turn.turn_id,
                    ("Срок нельзя проверить по доступному календарю. Задача callback не сохранена; обращение оставлено для ручной обработки.",),
                    code="CALLBACK_CALENDAR_UNAVAILABLE", ended=True)
            if deadline > draft.deadline + timedelta(minutes=5):
                updated = draft.model_copy(update={
                    "deadline": deadline,
                    "last_turn_id": turn.turn_id,
                    "version": draft.version + 1,
                })
                await self.store.save_callback_draft(updated, expected_version=draft.version)
                return self._callback_prompt(call_id, turn.turn_id, updated,
                    (*self._confirmation_lines(request, updated),
                     "Срок изменился за время разговора. Подтвердите его ещё раз."))
            task = CallbackTask(
                task_id=uuid4().hex,
                request_id=request.request_id,
                state=CallbackState.QUEUED,
                priority=request.priority,
                summary=draft.summary,
                consented_at=now,
                deadline=deadline,
                preferred_at=draft.preferred_at,
                contact_ciphertext=draft.contact_ciphertext,
                contact_masked=draft.contact_masked,
                endpoint=self.settings.callback_endpoint,
                next_at=max(now, draft.preferred_at or now),
                consent_turn_id=turn.turn_id,
                consent_input_epoch=draft.input_epoch,
                consent_draft_version=draft.version,
            )
            try:
                saved = await self.store.create_callback_task(
                    task,
                    draft_version=draft.version,
                    consent_turn_id=turn.turn_id,
                    input_epoch=draft.input_epoch,
                )
            except HandoffConflict:
                current = await self.store.get_callback_task_by_request(request.request_id)
                if current is not None and current.consent_turn_id == turn.turn_id:
                    saved = current
                else:
                    return self._callback_reply(call_id, turn.turn_id,
                        ("Подтверждение устарело; задача не сохранена. Повторите ответ.",),
                        code="CALLBACK_STALE")
            when = saved.preferred_at or saved.deadline
            return self._callback_reply(call_id, turn.turn_id,
                (f"Задача сохранена в очереди сотрудника. Ожидаемый срок — до {format_clinic(when)}.",
                 "Всего доброго."), code="CALLBACK_QUEUED", ended=True)
        return self._callback_prompt(call_id, turn.turn_id, draft)

    async def _retry_callback_input(self, call_id, turn, request, draft, phrases):
        invalid_turns = draft.invalid_turns + 1
        if invalid_turns >= 3:
            await self.store.delete_callback_draft(
                request.request_id, expected_version=draft.version
            )
            current = await self.store.get_request(request.request_id)
            if current is not None:
                await self._mark_request(current, callback_state=CallbackState.CANCELLED)
            return self._callback_reply(call_id, turn.turn_id,
                ("Не удалось оформить обратный звонок. Задача не сохранена.", "Всего доброго."),
                code="CALLBACK_UNDERSTANDING_LIMIT", ended=True)
        updated = draft.model_copy(update={
            "invalid_turns": invalid_turns,
            "last_turn_id": turn.turn_id,
            "version": draft.version + 1,
        })
        await self.store.save_callback_draft(updated, expected_version=draft.version)
        return self._callback_prompt(call_id, turn.turn_id, updated, phrases)

    def _confirmation_lines(self, request: HandoffRequest, draft: CallbackDraft) -> tuple[str, ...]:
        contact = self._open_contact(draft.contact_ciphertext)
        preferred = (
            f"Предпочтение: {format_clinic(draft.preferred_at)}."
            if draft.preferred_at else "Звонок — в ближайшее возможное время."
        )
        return (
            f"Обращение: {draft.summary or request.summary}",
            f"Номер {contact or draft.contact_masked}.",
            preferred,
            f"Нормативный срок: до {format_clinic(draft.deadline)}.",
            "Сохранить задачу?",
        )

    def _callback_prompt(self, call_id, turn_id, draft, phrases=None):
        state = DialogueState(call_id=call_id, phase=Phase.HANDOFF)
        return reply(state, turn_id, *(phrases or ("Продолжим оформление обратного звонка.",)),
                     code="CALLBACK_STEP").model_copy(update={"transfer": None})

    def _callback_reply(self, call_id, turn_id, phrases, *, code, ended=False):
        state = DialogueState(call_id=call_id, phase=Phase.ENDED if ended else Phase.HANDOFF)
        return reply(state, turn_id, *phrases, code=code).model_copy(update={"transfer": None})

    def _open_contact(self, token):
        if not token:
            return None
        try:
            return self.cipher.decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeError):
            return None

    async def discard_draft(self, call_id: str):
        await self.store.revoke_callback_draft(call_id)

    async def operator_claim(self, task_id, owner):
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        task = await self.store.get_callback_task(task_id)
        if task is None:
            raise HandoffConflict("callback task not found")
        return await self.store.claim_callback_task(
            task_id, owner, task.version, now, now + timedelta(minutes=10)
        )

    async def operator_start_attempt(self, task_id, owner, version):
        task = await self.store.get_callback_task(task_id)
        if task is None:
            raise HandoffConflict("задача не найдена")
        attempt = CallbackAttempt(
            attempt_id=uuid4().hex, task_id=task_id,
            started_at=self.clock.now().astimezone(CLINIC_TIMEZONE),
            owner=owner,
        )
        return await self.store.start_callback_attempt(
            attempt, expected_version=version,
            now=self.clock.now().astimezone(CLINIC_TIMEZONE),
        )

    async def operator_finish_attempt(
        self, attempt_id, outcome, *, owner: str | None = None,
        version: int | None = None, reason=None
    ):
        if owner is None or version is None:
            # Internal compatibility for the original service-level tests. The HTTP
            # API always requires both fields and validates the stored owner/version.
            attempt = await self.store.get_callback_attempt(attempt_id)
            task = (await self.store.get_callback_task(attempt.task_id)) if attempt else None
            if attempt is None or task is None:
                raise HandoffConflict("попытка callback не найдена")
            owner = owner or attempt.owner
            version = task.version if version is None else version
        return await self.store.finish_callback_attempt(
            attempt_id, outcome, self.clock.now().astimezone(CLINIC_TIMEZONE), reason,
            owner=owner, expected_version=version,
        )

    async def operator_cancel(self, task_id, owner, version):
        return await self.store.cancel_callback_task(
            task_id, owner, version, self.clock.now().astimezone(CLINIC_TIMEZONE)
        )

    async def callback_attempt_history(self, task_id):
        return await self.store.callback_attempts_for_task(task_id)

    async def recover_callbacks_once(self):
        return await self.store.recover_callback_tasks(
            self.clock.now().astimezone(CLINIC_TIMEZONE)
        )

    async def recover_missing_mis_intents_once(self):
        """Seed legacy connected rows lacking an intent; never repeats a POST."""
        recovered = 0
        for offset in range(0, 500, 50):
            records = await self.store.list_requests(limit=50, offset=offset)
            for record in records:
                if record.transfer_state != TransferState.CONNECTED or record.mis_operation_id:
                    continue
                if record.priority == "P1":
                    client_id = self.settings.technical_client_id
                    kind = "emergency_notice"
                elif record.identity_status == "verified" and record.client_id is not None:
                    client_id = record.client_id
                    kind = "transfer"
                else:
                    await self._mark_request(record, mis_state=MisDeliveryState.MANUAL)
                    continue
                if client_id is not None:
                    await self._prepare_operation(record, kind, client_id)
                    recovered += 1
            if len(records) < 50:
                break
        return recovered

    def reveal_contact(self, task: CallbackTask) -> str | None:
        return self._open_contact(task.contact_ciphertext)

    async def reveal_contact_for_owner(self, task_id: str, owner: str) -> str | None:
        task = await self.store.get_callback_task(task_id)
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        if (task is None or task.owner != owner
                or task.state not in {CallbackState.CLAIMED, CallbackState.IN_PROGRESS}
                or task.lease_until is None or task.lease_until <= now):
            raise HandoffConflict("задача не принадлежит активному владельцу")
        return self.reveal_contact(task)

    async def deliver_mis_once(self, *, limit: int = 1) -> int:
        """Ограниченная отправка/сверка. Не вызывается на телефонном критическом пути."""
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        operations = await self.store.due_mis_operations(now, min(10, max(1, limit)))
        processed = 0
        for operation in operations:
            try:
                if operation.state == MisDeliveryState.PENDING:
                    sending = operation.model_copy(update={
                        "state": MisDeliveryState.SENDING,
                        "sending_at": now,
                        "next_check_at": now + timedelta(seconds=45),
                        "version": operation.version + 1,
                    })
                    operation = await self.store.update_mis_operation(
                        sending, expected_version=operation.version
                    )
                    # CAS-проигравший worker увидит конфликт и не имеет права посылать второй POST.
                    with demand(Priority.BACKGROUND, until=now + timedelta(seconds=2),
                                owner=operation.operation_id):
                        result = await self.mis.create_escalation(
                            EscalationCreate(client_id=operation.client_id,
                                             description=operation.description)
                        )
                    applied = operation.model_copy(update={
                        "state": MisDeliveryState.APPLIED,
                        "escalation_id": result.id,
                        "next_check_at": None,
                        "reason": None,
                        "version": operation.version + 1,
                    })
                    await self.store.update_mis_operation(applied,
                                                          expected_version=operation.version)
                elif operation.state in {MisDeliveryState.UNKNOWN, MisDeliveryState.SENDING}:
                    if operation.state == MisDeliveryState.SENDING:
                        unknown = operation.model_copy(update={
                            "state": MisDeliveryState.UNKNOWN,
                            "reason": "SENDING_RESULT_LOST",
                            "next_check_at": now,
                            "version": operation.version + 1,
                        })
                        operation = await self.store.update_mis_operation(
                            unknown, expected_version=operation.version
                        )
                    await self._reconcile_mis(operation, now)
                processed += 1
            except HandoffConflict:
                # Другой процесс выиграл CAS; в этом цикле ничего не повторяем.
                continue
            except BudgetExhausted as error:
                current = await self.store.get_mis_operation(operation.operation_id)
                if current is not None and current.state == MisDeliveryState.SENDING:
                    pending = current.model_copy(update={
                        "state": MisDeliveryState.PENDING,
                        "sending_at": None,
                        "next_check_at": now + timedelta(seconds=error.retry_after or 15),
                        "reason": "BUDGET_NOT_SENT",
                        "version": current.version + 1,
                    })
                    await self.store.update_mis_operation(pending,
                                                          expected_version=current.version)
            except MisError as error:
                current = await self.store.get_mis_operation(operation.operation_id)
                if current is None:
                    continue
                if error.code == "RATE_LIMIT" and current.state == MisDeliveryState.SENDING:
                    # Внешний 429 означает, что эта команда отклонена до создания карточки.
                    pending = current.model_copy(update={
                        "state": MisDeliveryState.PENDING,
                        "sending_at": None,
                        "next_check_at": now + timedelta(seconds=error.retry_after or 60),
                        "reason": "RATE_LIMIT",
                        "version": current.version + 1,
                    })
                    await self.store.update_mis_operation(pending,
                                                          expected_version=current.version)
                elif isinstance(error, MisOutcomeUnknown):
                    unknown = current.model_copy(update={
                        "state": MisDeliveryState.UNKNOWN,
                        "checks": 0,
                        "next_check_at": now + timedelta(seconds=15),
                        "reason": error.code,
                        "version": current.version + 1,
                    })
                    await self.store.update_mis_operation(unknown,
                                                          expected_version=current.version)
                else:
                    rejected = current.model_copy(update={
                        "state": MisDeliveryState.REJECTED,
                        "next_check_at": None,
                        "reason": error.code,
                        "version": current.version + 1,
                    })
                    await self.store.update_mis_operation(rejected,
                                                          expected_version=current.version)
            except Exception:
                # Неизвестная ошибка после состояния sending — только сверка, никогда POST-повтор.
                current = await self.store.get_mis_operation(operation.operation_id)
                if current and current.state == MisDeliveryState.SENDING:
                    unknown = current.model_copy(update={
                        "state": MisDeliveryState.UNKNOWN,
                        "next_check_at": now + timedelta(seconds=15),
                        "reason": "DELIVERY_ERROR",
                        "version": current.version + 1,
                    })
                    try:
                        await self.store.update_mis_operation(unknown,
                                                              expected_version=current.version)
                    except HandoffConflict:
                        pass
        return processed

    async def _reconcile_mis(self, operation: MisOperation, now: datetime) -> None:
        if (operation.marker != f"T08:{operation.operation_id}"
                or re.fullmatch(r"T08:[0-9a-f]{32}", operation.marker) is None):
            current = await self.store.get_mis_operation(operation.operation_id)
            if current is not None:
                manual = current.model_copy(update={
                    "state": MisDeliveryState.MANUAL,
                    "reason": "INVALID_MARKER",
                    "next_check_at": None,
                    "version": current.version + 1,
                })
                await self.store.update_mis_operation(manual, expected_version=current.version)
            return
        matches = []
        seen: dict[int, object] = {}
        unstable = False
        complete = False
        marker_conflict = False
        try:
            with demand(Priority.RECONCILE, until=now + timedelta(seconds=3),
                        owner=operation.operation_id):
                for page_number in range(3):
                    page = await self.mis.escalations(
                        operation.client_id, limit=50, offset=page_number * 50
                    )
                    for item in page:
                        previous = seen.get(item.id)
                        if previous is not None:
                            if previous != item:
                                unstable = True
                            else:
                                unstable = True
                            continue
                        seen[item.id] = item
                        if operation.marker in item.description:
                            if (
                                item.client_id != operation.client_id
                                or item.description != operation.description
                                or not item.description.startswith(f"[{operation.marker}] ")
                            ):
                                marker_conflict = True
                            else:
                                matches.append(item)
                    if len(page) < 50:
                        complete = True
                        break
        except (MisError, MisUnavailable):
            await self._defer_reconciliation(operation, now, "RECONCILE_UNAVAILABLE")
            return
        current = await self.store.get_mis_operation(operation.operation_id)
        if current is None:
            return
        if marker_conflict:
            state, reason, escalation_id = MisDeliveryState.MANUAL, "RECONCILE_MARKER_MISMATCH", None
        elif len(matches) > 1:
            state, reason, escalation_id = MisDeliveryState.MANUAL, "DUPLICATE_MARKER", None
        elif unstable or not complete:
            state, reason, escalation_id = MisDeliveryState.MANUAL, "RECONCILE_INCOMPLETE", None
        elif len(matches) == 1:
            match = matches[0]
            if match.client_id != operation.client_id or match.description != operation.description:
                state, reason, escalation_id = MisDeliveryState.MANUAL, "RECONCILE_MISMATCH", None
            else:
                state, reason, escalation_id = MisDeliveryState.APPLIED, None, match.id
        elif current.checks >= 2:
            state, reason, escalation_id = MisDeliveryState.MANUAL, "RECONCILE_INCOMPLETE", None
        else:
            state, reason, escalation_id = MisDeliveryState.UNKNOWN, "NOT_VISIBLE_YET", None
        next_at = now + timedelta(seconds=20) if state == MisDeliveryState.UNKNOWN else None
        updated = current.model_copy(update={
            "state": state, "reason": reason, "escalation_id": escalation_id,
            "checks": current.checks + 1, "next_check_at": next_at,
            "version": current.version + 1,
        })
        await self.store.update_mis_operation(updated, expected_version=current.version)

    async def _defer_reconciliation(self, operation, now, reason):
        current = await self.store.get_mis_operation(operation.operation_id)
        if current is None:
            return
        if current.checks >= 2:
            state, due = MisDeliveryState.MANUAL, None
        else:
            state, due = MisDeliveryState.UNKNOWN, now + timedelta(seconds=30)
        updated = current.model_copy(update={
            "state": state, "reason": reason, "checks": current.checks + 1,
            "next_check_at": due, "version": current.version + 1,
        })
        await self.store.update_mis_operation(updated, expected_version=current.version)


class HandoffWorker:
    """Один ограниченный фоновый цикл для durable MIS-delivery; не создаёт отдельный бюджет."""

    def __init__(self, service: HandoffService, poll_s: float = 1.0):
        self.service, self.poll_s = service, max(0.1, min(30.0, poll_s))
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        try:
            await self.service.recover_callbacks_once()
            await self.service.recover_missing_mis_intents_once()
        except Exception:
            pass
        while True:
            try:
                await self.service.recover_callbacks_once()
                await self.service.deliver_mis_once(limit=1)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.poll_s)


def format_clinic(value: datetime) -> str:
    value = value.astimezone(CLINIC_TIMEZONE)
    return f"{value.day:02}.{value.month:02} в {value:%H:%M}"


def parse_preferred_time(text: str, now: datetime) -> datetime | None:
    from app.nlu.parsers.dates import parse_dates
    from app.nlu.parsers.times import parse_times

    dates = parse_dates(text, now.date())
    times = parse_times(text, bare=True)
    if "date_from" not in dates or "time_from" not in times:
        return None
    day = dates["date_from"][0]
    at = times["time_from"][0]
    try:
        result = datetime.combine(day, at, CLINIC_TIMEZONE)
    except (TypeError, ValueError):
        return None
    return result if result > now else None
