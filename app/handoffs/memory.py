"""Потокобезопасный store для тестов и автономной демонстрации."""

import asyncio

from app.handoffs.models import (
    CallbackAttempt,
    CallbackDraft,
    CallbackOutcome,
    CallbackState,
    CallbackTask,
    HandoffRequest,
    MisDeliveryState,
    MisOperation,
    TransferAttempt,
    TransferState,
)
from app.handoffs.ports import HandoffConflict


class MemoryHandoffStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.requests: dict[str, HandoffRequest] = {}
        self.calls: dict[str, str] = {}
        self.attempts: dict[str, TransferAttempt] = {}
        self.mis_operations: dict[str, MisOperation] = {}
        self.callback_drafts: dict[str, CallbackDraft] = {}
        self.callback_tasks: dict[str, CallbackTask] = {}
        self.callback_by_request: dict[str, str] = {}
        self.callback_attempts: dict[str, CallbackAttempt] = {}

    async def create_request(self, request):
        async with self._lock:
            existing_id = self.calls.get(request.call_id)
            if existing_id:
                existing = self.requests[existing_id]
                changes = {
                    "route": request.route,
                    "priority": request.priority,
                    "purpose": request.purpose,
                    "summary": request.summary,
                    "identity_status": (
                        "verified"
                        if request.identity_status == "verified"
                        else existing.identity_status
                    ),
                    "client_id": request.client_id or existing.client_id,
                    "recording_opt_out": (
                        request.recording_opt_out
                        if request.recording_opt_out is not None
                        else existing.recording_opt_out
                    ),
                }
                if request.priority == "P1" and existing.mis_operation_id is None:
                    changes["mis_state"] = MisDeliveryState.MANUAL
                if any(getattr(existing, key) != value for key, value in changes.items()):
                    existing = existing.model_copy(
                        update={**changes, "version": existing.version + 1}
                    )
                    self.requests[existing_id] = existing
                return existing
            self.requests[request.request_id] = request
            self.calls[request.call_id] = request.request_id
            return request

    async def get_request(self, request_id):
        async with self._lock:
            return self.requests.get(request_id)

    async def request_for_call(self, call_id):
        async with self._lock:
            request_id = self.calls.get(call_id)
            return self.requests.get(request_id) if request_id else None

    async def list_requests(self, *, limit, offset, state=None):
        async with self._lock:
            items = list(self.requests.values())
            if state:
                items = [r for r in items if r.transfer_state.value == state]
            items.sort(key=lambda r: (r.created_at, r.request_id), reverse=True)
            return items[offset : offset + limit]

    async def begin_attempt(self, attempt):
        async with self._lock:
            current = self.attempts.get(attempt.attempt_id)
            if current:
                if (
                    current.request_id,
                    current.call_id,
                    current.route,
                    current.priority,
                ) != (
                    attempt.request_id,
                    attempt.call_id,
                    attempt.route,
                    attempt.priority,
                ):
                    raise HandoffConflict("attempt_id уже использован с другим содержимым")
                return current
            request = self.requests.get(attempt.request_id)
            if request is None or request.call_id != attempt.call_id:
                raise HandoffConflict("карточка звонка не найдена")
            self.attempts[attempt.attempt_id] = attempt
            self.requests[request.request_id] = request.model_copy(
                update={"attempt_id": attempt.attempt_id,
                        "transfer_state": TransferState.ATTEMPTING,
                        "transfer_reason": None,
                        "version": request.version + 1}
            )
            return attempt

    async def finish_attempt(self, attempt_id, outcome, finished_at, reason):
        async with self._lock:
            attempt = self.attempts.get(attempt_id)
            if attempt is None:
                raise HandoffConflict("попытка перевода не найдена")
            if attempt.outcome != TransferState.ATTEMPTING:
                if (attempt.outcome, attempt.reason) != (outcome, reason):
                    raise HandoffConflict("терминальный исход попытки уже сохранён")
                return self.requests[attempt.request_id], attempt
            updated = attempt.model_copy(
                update={"outcome": outcome, "reason": reason, "finished_at": finished_at}
            )
            self.attempts[attempt_id] = updated
            request = self.requests[attempt.request_id]
            if request.attempt_id == attempt_id:
                state = outcome
                mis_state = request.mis_state
                if outcome == TransferState.CONNECTED:
                    mis_state = MisDeliveryState.PENDING
                request = request.model_copy(
                    update={"transfer_state": state, "transfer_reason": reason,
                            "mis_state": mis_state, "version": request.version + 1}
                )
                self.requests[request.request_id] = request
            return request, updated

    async def save_request(self, request, *, expected_version):
        async with self._lock:
            current = self.requests.get(request.request_id)
            if current is None or current.version != expected_version:
                raise HandoffConflict("версия карточки изменилась")
            updated = request.model_copy(update={"version": expected_version + 1})
            self.requests[request.request_id] = updated
            return updated

    async def create_mis_operation(self, operation):
        async with self._lock:
            existing = self.mis_operations.get(operation.operation_id)
            if existing:
                if existing != operation:
                    raise HandoffConflict("operation_id уже использован с другим содержимым")
                return existing
            if any(o.marker == operation.marker for o in self.mis_operations.values()):
                raise HandoffConflict("marker уже привязан к другой операции")
            if any(o.request_id == operation.request_id for o in self.mis_operations.values()):
                raise HandoffConflict("request already has a MIS operation")
            self.mis_operations[operation.operation_id] = operation
            return operation

    async def get_mis_operation(self, operation_id):
        async with self._lock:
            return self.mis_operations.get(operation_id)

    async def update_mis_operation(self, operation, *, expected_version):
        async with self._lock:
            current = self.mis_operations.get(operation.operation_id)
            if current is None or current.version != expected_version:
                raise HandoffConflict("версия MIS-операции изменилась")
            updated = operation.model_copy(update={"version": expected_version + 1})
            self.mis_operations[operation.operation_id] = updated
            request = self.requests[updated.request_id]
            self.requests[request.request_id] = request.model_copy(
                update={"mis_state": updated.state, "mis_operation_id": updated.operation_id,
                        "mis_escalation_id": updated.escalation_id,
                        "version": request.version + 1}
            )
            return updated

    async def due_mis_operations(self, now, limit):
        async with self._lock:
            items = [
                o for o in self.mis_operations.values()
                if o.state.value in {"pending", "unknown", "sending"}
                and (o.next_check_at is None or o.next_check_at <= now)
            ]
            return sorted(items, key=lambda o: (o.created_at, o.operation_id))[:limit]

    async def save_callback_draft(self, draft):
        async with self._lock:
            if draft.request_id not in self.requests:
                raise HandoffConflict("карточка не найдена")
            self.callback_drafts[draft.request_id] = draft
            return draft

    async def get_callback_draft(self, request_id):
        async with self._lock:
            return self.callback_drafts.get(request_id)

    async def delete_callback_draft(self, request_id):
        async with self._lock:
            return self.callback_drafts.pop(request_id, None)

    async def create_callback_task(self, task):
        async with self._lock:
            existing_id = self.callback_by_request.get(task.request_id)
            if existing_id:
                return self.callback_tasks[existing_id]
            self.callback_tasks[task.task_id] = task
            self.callback_by_request[task.request_id] = task.task_id
            self.callback_drafts.pop(task.request_id, None)
            request = self.requests[task.request_id]
            self.requests[request.request_id] = request.model_copy(
                update={"callback_state": task.state, "callback_deadline": task.deadline,
                        "callback_preferred_at": task.preferred_at, "version": request.version + 1}
            )
            return task

    async def get_callback_task(self, task_id):
        async with self._lock:
            return self.callback_tasks.get(task_id)

    async def get_callback_task_by_request(self, request_id):
        async with self._lock:
            task_id = self.callback_by_request.get(request_id)
            return self.callback_tasks.get(task_id) if task_id else None

    async def list_callback_tasks(self, *, limit, offset, state=None):
        async with self._lock:
            items = list(self.callback_tasks.values())
            if state:
                items = [t for t in items if t.state.value == state]
            items.sort(key=lambda t: (t.next_at, t.task_id))
            return items[offset : offset + limit]

    async def claim_callback_task(self, task_id, owner, expected_version, now, lease_until):
        async with self._lock:
            task = self.callback_tasks.get(task_id)
            if task is None or task.version != expected_version:
                raise HandoffConflict("задача уже изменилась")
            if task.state == CallbackState.CLAIMED and task.lease_until and task.lease_until > now:
                raise HandoffConflict("задачу уже взял другой сотрудник")
            if task.state not in {CallbackState.QUEUED, CallbackState.CLAIMED}:
                raise HandoffConflict("задача недоступна для взятия")
            if task.next_at > now:
                raise HandoffConflict("срок следующего действия ещё не наступил")
            updated = task.model_copy(update={"state": CallbackState.CLAIMED,
                                              "owner": owner, "lease_until": lease_until,
                                              "version": task.version + 1})
            self.callback_tasks[task_id] = updated
            return updated

    async def start_callback_attempt(self, attempt, *, expected_version):
        async with self._lock:
            task = self.callback_tasks.get(attempt.task_id)
            if task is None or task.version != expected_version or task.owner != attempt.owner:
                raise HandoffConflict("задачу нельзя начать в изменённом состоянии")
            if task.state != CallbackState.CLAIMED or task.attempts >= 3:
                raise HandoffConflict("недопустимое состояние или исчерпаны попытки")
            if attempt.attempt_id in self.callback_attempts:
                old = self.callback_attempts[attempt.attempt_id]
                if old != attempt:
                    raise HandoffConflict("attempt_id уже использован")
                return task, old
            self.callback_attempts[attempt.attempt_id] = attempt
            updated = task.model_copy(update={"state": CallbackState.IN_PROGRESS,
                                              "attempts": task.attempts + 1,
                                              "version": task.version + 1})
            self.callback_tasks[task.task_id] = updated
            return updated, attempt

    async def finish_callback_attempt(self, attempt_id, outcome, finished_at, reason):
        async with self._lock:
            attempt = self.callback_attempts.get(attempt_id)
            if attempt is None:
                raise HandoffConflict("попытка callback не найдена")
            if attempt.finished_at is not None:
                if attempt.outcome.value != outcome or attempt.reason != reason:
                    raise HandoffConflict("исход попытки уже сохранён")
                return self.callback_tasks[attempt.task_id]
            outcome = CallbackOutcome(outcome)
            attempt = attempt.model_copy(update={"outcome": outcome,
                                                 "finished_at": finished_at,
                                                 "reason": reason})
            self.callback_attempts[attempt_id] = attempt
            task = self.callback_tasks[attempt.task_id]
            if outcome == CallbackOutcome.CONNECTED:
                state, next_at, sms = CallbackState.DONE, finished_at, False
            elif outcome == CallbackOutcome.CANCELLED:
                state, next_at, sms = CallbackState.CANCELLED, finished_at, False
            elif outcome == CallbackOutcome.UNKNOWN:
                state, next_at, sms = CallbackState.MANUAL, finished_at, False
            elif task.attempts >= 3:
                state, next_at, sms = CallbackState.EXHAUSTED, finished_at, True
            else:
                # Межпопыточный интервал — рабочее решение, не норматив SLA.
                state, next_at, sms = CallbackState.QUEUED, finished_at, False
                from datetime import timedelta
                next_at = finished_at + timedelta(minutes=5)
            task = task.model_copy(update={"state": state, "next_at": next_at,
                                           "last_outcome": outcome, "sms_pending": sms,
                                           "owner": None, "lease_until": None,
                                           "version": task.version + 1})
            self.callback_tasks[task.task_id] = task
            request = self.requests[task.request_id]
            self.requests[request.request_id] = request.model_copy(
                update={"callback_state": state, "callback_attempts": task.attempts,
                        "callback_sms_pending": sms, "version": request.version + 1}
            )
            return task

    async def cancel_callback_task(self, task_id, owner, expected_version):
        async with self._lock:
            task = self.callback_tasks.get(task_id)
            if task is None or task.version != expected_version:
                raise HandoffConflict("задача уже изменилась")
            if task.owner not in {None, owner} or task.state == CallbackState.IN_PROGRESS:
                raise HandoffConflict("задача исполняется другим сотрудником")
            updated = task.model_copy(update={"state": CallbackState.CANCELLED,
                                              "owner": None, "lease_until": None,
                                              "version": task.version + 1})
            self.callback_tasks[task_id] = updated
            request = self.requests[task.request_id]
            self.requests[request.request_id] = request.model_copy(
                update={"callback_state": CallbackState.CANCELLED,
                        "version": request.version + 1}
            )
            return updated
