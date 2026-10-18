"""PostgreSQL-очередь T08: короткие транзакции, версии и leases, без сетевого I/O."""


from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

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
from app.storage.journal import journal_transaction
from app.storage.schema import (
    callback_attempts,
    callback_drafts,
    callback_tasks,
    handoff_attempts,
    handoff_mis_operations,
    handoff_requests,
)


def _data(model):
    return model.model_dump(mode="json")


def _request(row):
    model = HandoffRequest.model_validate(row.data)
    if (model.request_id, model.call_id, model.version) != (
        row.request_id,
        row.call_id,
        row.version,
    ):
        raise RuntimeError("проекция карточки T08 повреждена")
    return model


def _mis(row):
    model = MisOperation.model_validate(row.data)
    if (model.operation_id, model.state, model.next_check_at) != (
        row.operation_id,
        MisDeliveryState(row.state),
        row.next_check_at,
    ):
        raise RuntimeError("проекция MIS-операции T08 повреждена")
    return model


def _task(row):
    model = CallbackTask.model_validate(row.data)
    if (model.task_id, model.request_id, model.state, model.version) != (
        row.task_id,
        row.request_id,
        CallbackState(row.state),
        row.version,
    ):
        raise RuntimeError("проекция callback T08 повреждена")
    return model


class PostgresHandoffStore:
    def __init__(self, engine):
        self.engine = engine

    async def create_request(self, request):
        async with journal_transaction(self.engine) as connection:
            row = (
                await connection.execute(
                    select(handoff_requests).where(handoff_requests.c.call_id == request.call_id)
                    .with_for_update()
                )
            ).first()
            if row:
                current = _request(row)
                changes = {
                    "route": request.route,
                    "priority": request.priority,
                    "purpose": request.purpose,
                    "summary": request.summary,
                    "identity_status": (
                        "verified"
                        if request.identity_status == "verified"
                        else current.identity_status
                    ),
                    "client_id": request.client_id or current.client_id,
                    "recording_opt_out": (
                        request.recording_opt_out
                        if request.recording_opt_out is not None
                        else current.recording_opt_out
                    ),
                }
                if request.priority == "P1" and current.mis_operation_id is None:
                    changes["mis_state"] = MisDeliveryState.MANUAL
                if not any(getattr(current, key) != value for key, value in changes.items()):
                    return current
                updated = current.model_copy(update={**changes, "version": current.version + 1})
                await self._save_request(connection, updated)
                return updated
            await connection.execute(
                insert(handoff_requests).values(
                    request_id=request.request_id, call_id=request.call_id,
                    created_at=request.created_at, route=request.route,
                    priority=request.priority, transfer_state=request.transfer_state.value,
                    mis_state=request.mis_state.value,
                    callback_state=request.callback_state.value if request.callback_state else None,
                    callback_deadline=request.callback_deadline, version=request.version,
                    data=_data(request),
                )
            )
            return request

    async def _read_request(self, connection, request_id, *, lock=False):
        query = select(handoff_requests).where(handoff_requests.c.request_id == request_id)
        if lock:
            query = query.with_for_update()
        row = (await connection.execute(query)).first()
        return None if row is None else _request(row)

    async def _save_request(self, connection, request):
        result = await connection.execute(
            update(handoff_requests)
            .where(handoff_requests.c.request_id == request.request_id)
            .values(
                route=request.route, priority=request.priority,
                transfer_state=request.transfer_state.value, mis_state=request.mis_state.value,
                callback_state=request.callback_state.value if request.callback_state else None,
                callback_deadline=request.callback_deadline, version=request.version,
                data=_data(request),
            )
        )
        if result.rowcount != 1:
            raise HandoffConflict("карточка обращения не найдена")

    async def get_request(self, request_id):
        async with self.engine.connect() as connection:
            return await self._read_request(connection, request_id)

    async def request_for_call(self, call_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(handoff_requests).where(handoff_requests.c.call_id == call_id)
            )).first()
            return None if row is None else _request(row)

    async def list_requests(self, *, limit, offset, state=None):
        query = select(handoff_requests).order_by(
            handoff_requests.c.created_at.desc(), handoff_requests.c.request_id.desc()
        ).limit(limit).offset(offset)
        if state:
            query = query.where(handoff_requests.c.transfer_state == state)
        async with self.engine.connect() as connection:
            return [_request(r) for r in (await connection.execute(query)).all()]

    async def begin_attempt(self, attempt):
        async with journal_transaction(self.engine) as connection:
            request = await self._read_request(connection, attempt.request_id, lock=True)
            if request is None or request.call_id != attempt.call_id:
                raise HandoffConflict("карточка звонка не найдена")
            row = (await connection.execute(
                select(handoff_attempts).where(handoff_attempts.c.attempt_id == attempt.attempt_id)
                .with_for_update()
            )).first()
            if row:
                old = TransferAttempt.model_validate(row.data)
                if (
                    old.request_id,
                    old.call_id,
                    old.route,
                    old.priority,
                ) != (
                    attempt.request_id,
                    attempt.call_id,
                    attempt.route,
                    attempt.priority,
                ):
                    raise HandoffConflict("attempt_id уже использован с другим содержимым")
                return old
            await connection.execute(insert(handoff_attempts).values(
                attempt_id=attempt.attempt_id, request_id=attempt.request_id,
                started_at=attempt.started_at, finished_at=None,
                outcome=TransferState.ATTEMPTING.value, data=_data(attempt),
            ))
            request = request.model_copy(update={
                "attempt_id": attempt.attempt_id,
                "transfer_state": TransferState.ATTEMPTING,
                "transfer_reason": None,
                "version": request.version + 1,
            })
            await self._save_request(connection, request)
            return attempt

    async def finish_attempt(self, attempt_id, outcome, finished_at, reason):
        async with journal_transaction(self.engine) as connection:
            row = (await connection.execute(
                select(handoff_attempts).where(handoff_attempts.c.attempt_id == attempt_id)
                .with_for_update()
            )).first()
            if row is None:
                raise HandoffConflict("попытка перевода не найдена")
            attempt = TransferAttempt.model_validate(row.data)
            request = await self._read_request(connection, attempt.request_id, lock=True)
            if attempt.outcome != TransferState.ATTEMPTING:
                if (attempt.outcome, attempt.reason) != (outcome, reason):
                    raise HandoffConflict("терминальный исход попытки уже сохранён")
                return request, attempt
            attempt = attempt.model_copy(update={
                "outcome": outcome, "reason": reason, "finished_at": finished_at,
            })
            await connection.execute(update(handoff_attempts)
                .where(handoff_attempts.c.attempt_id == attempt_id)
                .values(outcome=outcome.value, finished_at=finished_at, data=_data(attempt)))
            if request.attempt_id == attempt_id:
                request = request.model_copy(update={
                    "transfer_state": outcome, "transfer_reason": reason,
                    "mis_state": MisDeliveryState.PENDING
                    if outcome == TransferState.CONNECTED else request.mis_state,
                    "version": request.version + 1,
                })
                await self._save_request(connection, request)
            return request, attempt

    async def save_request(self, request, *, expected_version):
        async with journal_transaction(self.engine) as connection:
            current = await self._read_request(connection, request.request_id, lock=True)
            if current is None or current.version != expected_version:
                raise HandoffConflict("версия карточки изменилась")
            updated = request.model_copy(update={"version": expected_version + 1})
            await self._save_request(connection, updated)
            return updated

    async def create_mis_operation(self, operation):
        async with journal_transaction(self.engine) as connection:
            request = await self._read_request(connection, operation.request_id, lock=True)
            if request is None:
                raise HandoffConflict("handoff request not found")
            if request.mis_operation_id:
                existing_row = (await connection.execute(select(handoff_mis_operations).where(
                    handoff_mis_operations.c.operation_id == request.mis_operation_id
                ))).first()
                if existing_row is not None:
                    return _mis(existing_row)
                raise HandoffConflict("request already has a MIS operation")
            row = (await connection.execute(
                select(handoff_mis_operations).where(
                    handoff_mis_operations.c.operation_id == operation.operation_id
                ).with_for_update()
            )).first()
            if row:
                old = _mis(row)
                if old != operation:
                    raise HandoffConflict("operation_id уже использован с другим содержимым")
                return old
            await connection.execute(insert(handoff_mis_operations).values(
                operation_id=operation.operation_id, request_id=operation.request_id,
                kind=operation.kind, state=operation.state.value,
                next_check_at=operation.next_check_at, data=_data(operation),
            ))
            request = await self._read_request(connection, operation.request_id, lock=True)
            if request is None:
                raise HandoffConflict("карточка не найдена")
            request = request.model_copy(update={
                "mis_state": operation.state, "mis_operation_id": operation.operation_id,
                "version": request.version + 1,
            })
            await self._save_request(connection, request)
            return operation

    async def get_mis_operation(self, operation_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(handoff_mis_operations).where(
                handoff_mis_operations.c.operation_id == operation_id
            ))).first()
            return None if row is None else _mis(row)

    async def update_mis_operation(self, operation, *, expected_version):
        async with journal_transaction(self.engine) as connection:
            row = (await connection.execute(select(handoff_mis_operations).where(
                handoff_mis_operations.c.operation_id == operation.operation_id
            ).with_for_update())).first()
            if row is None:
                raise HandoffConflict("MIS-операция не найдена")
            current = _mis(row)
            if current.version != expected_version:
                raise HandoffConflict("версия MIS-операции изменилась")
            updated = operation.model_copy(update={"version": expected_version + 1})
            await connection.execute(update(handoff_mis_operations).where(
                handoff_mis_operations.c.operation_id == updated.operation_id
            ).values(state=updated.state.value, next_check_at=updated.next_check_at,
                     data=_data(updated)))
            request = await self._read_request(connection, updated.request_id, lock=True)
            request = request.model_copy(update={
                "mis_state": updated.state, "mis_operation_id": updated.operation_id,
                "mis_escalation_id": updated.escalation_id,
                "version": request.version + 1,
            })
            await self._save_request(connection, request)
            return updated

    async def due_mis_operations(self, now, limit):
        query = select(handoff_mis_operations).where(
            handoff_mis_operations.c.state.in_(["pending", "unknown", "sending"]),
            (handoff_mis_operations.c.next_check_at.is_(None)
             | (handoff_mis_operations.c.next_check_at <= now)),
        ).order_by(handoff_mis_operations.c.next_check_at.nullsfirst()).limit(limit)
        async with self.engine.connect() as connection:
            return [_mis(r) for r in (await connection.execute(query)).all()]

    async def save_callback_draft(self, draft):
        async with journal_transaction(self.engine) as connection:
            if await self._read_request(connection, draft.request_id, lock=True) is None:
                raise HandoffConflict("карточка не найдена")
            await connection.execute(insert(callback_drafts).values(
                request_id=draft.request_id, stage=draft.stage,
                last_turn_id=draft.last_turn_id, version=draft.version, data=_data(draft),
            ).on_conflict_do_update(index_elements=[callback_drafts.c.request_id], set_={
                "stage": draft.stage, "last_turn_id": draft.last_turn_id,
                "version": draft.version, "data": _data(draft),
            }))
            return draft

    async def get_callback_draft(self, request_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(callback_drafts).where(
                callback_drafts.c.request_id == request_id
            ))).first()
            return None if row is None else CallbackDraft.model_validate(row.data)

    async def delete_callback_draft(self, request_id):
        async with journal_transaction(self.engine) as connection:
            await connection.execute(
                callback_drafts.delete().where(callback_drafts.c.request_id == request_id)
            )

    async def create_callback_task(self, task):
        async with journal_transaction(self.engine) as connection:
            row = (await connection.execute(select(callback_tasks).where(
                callback_tasks.c.request_id == task.request_id
            ).with_for_update())).first()
            if row:
                return _task(row)
            await connection.execute(insert(callback_tasks).values(
                task_id=task.task_id, request_id=task.request_id, state=task.state.value,
                priority=task.priority, deadline=task.deadline, next_at=task.next_at,
                owner=task.owner, lease_until=task.lease_until, version=task.version,
                data=_data(task),
            ))
            request = await self._read_request(connection, task.request_id, lock=True)
            request = request.model_copy(update={
                "callback_state": task.state, "callback_deadline": task.deadline,
                "callback_preferred_at": task.preferred_at,
                "version": request.version + 1,
            })
            await self._save_request(connection, request)
            await connection.execute(
                callback_drafts.delete().where(callback_drafts.c.request_id == task.request_id)
            )
            return task

    async def get_callback_task(self, task_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(callback_tasks).where(
                callback_tasks.c.task_id == task_id
            ))).first()
            return None if row is None else _task(row)

    async def get_callback_task_by_request(self, request_id):
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(callback_tasks).where(
                callback_tasks.c.request_id == request_id
            ))).first()
            return None if row is None else _task(row)

    async def list_callback_tasks(self, *, limit, offset, state=None):
        query = select(callback_tasks).order_by(callback_tasks.c.next_at, callback_tasks.c.task_id)
        if state:
            query = query.where(callback_tasks.c.state == state)
        query = query.limit(limit).offset(offset)
        async with self.engine.connect() as connection:
            return [_task(r) for r in (await connection.execute(query)).all()]

    async def _read_task(self, connection, task_id):
        row = (await connection.execute(select(callback_tasks).where(
            callback_tasks.c.task_id == task_id
        ).with_for_update())).first()
        return None if row is None else _task(row)

    async def _save_task(self, connection, task):
        await connection.execute(update(callback_tasks).where(
            callback_tasks.c.task_id == task.task_id
        ).values(state=task.state.value, next_at=task.next_at, owner=task.owner,
                 lease_until=task.lease_until, version=task.version, data=_data(task)))
        request = await self._read_request(connection, task.request_id, lock=True)
        if request is not None:
            request = request.model_copy(update={
                "callback_state": task.state,
                "callback_deadline": task.deadline,
                "callback_preferred_at": task.preferred_at,
                "callback_attempts": task.attempts,
                "callback_sms_pending": task.sms_pending,
                "version": request.version + 1,
            })
            await self._save_request(connection, request)

    async def claim_callback_task(self, task_id, owner, expected_version, now, lease_until):
        async with journal_transaction(self.engine) as connection:
            task = await self._read_task(connection, task_id)
            if task is None or task.version != expected_version:
                raise HandoffConflict("задача уже изменилась")
            if task.state == CallbackState.CLAIMED and task.lease_until and task.lease_until > now:
                raise HandoffConflict("задачу уже взял другой сотрудник")
            if task.state not in {CallbackState.QUEUED, CallbackState.CLAIMED} or task.next_at > now:
                raise HandoffConflict("задача недоступна")
            updated = task.model_copy(update={"state": CallbackState.CLAIMED, "owner": owner,
                "lease_until": lease_until, "version": task.version + 1})
            await self._save_task(connection, updated)
            return updated

    async def start_callback_attempt(self, attempt, *, expected_version):
        async with journal_transaction(self.engine) as connection:
            task = await self._read_task(connection, attempt.task_id)
            if task is None or task.version != expected_version or task.owner != attempt.owner:
                raise HandoffConflict("задачу нельзя начать в изменённом состоянии")
            if task.state != CallbackState.CLAIMED or task.attempts >= 3:
                raise HandoffConflict("недопустимое состояние или исчерпаны попытки")
            row = (await connection.execute(select(callback_attempts).where(
                callback_attempts.c.attempt_id == attempt.attempt_id
            ).with_for_update())).first()
            if row:
                old = CallbackAttempt.model_validate(row.data)
                if old != attempt:
                    raise HandoffConflict("attempt_id уже использован")
                return task, old
            await connection.execute(insert(callback_attempts).values(
                attempt_id=attempt.attempt_id, task_id=attempt.task_id,
                started_at=attempt.started_at, finished_at=None,
                outcome=attempt.outcome.value, data=_data(attempt)))
            updated = task.model_copy(update={"state": CallbackState.IN_PROGRESS,
                "attempts": task.attempts + 1, "version": task.version + 1})
            await self._save_task(connection, updated)
            return updated, attempt

    async def finish_callback_attempt(self, attempt_id, outcome, finished_at, reason):
        async with journal_transaction(self.engine) as connection:
            row = (await connection.execute(select(callback_attempts).where(
                callback_attempts.c.attempt_id == attempt_id
            ).with_for_update())).first()
            if row is None:
                raise HandoffConflict("попытка callback не найдена")
            attempt = CallbackAttempt.model_validate(row.data)
            task = await self._read_task(connection, attempt.task_id)
            if attempt.finished_at is not None:
                if (attempt.outcome.value, attempt.reason) != (outcome, reason):
                    raise HandoffConflict("исход попытки уже сохранён")
                return task
            result = CallbackOutcome(outcome)
            attempt = attempt.model_copy(update={"outcome": result, "finished_at": finished_at,
                "reason": reason})
            await connection.execute(update(callback_attempts).where(
                callback_attempts.c.attempt_id == attempt_id
            ).values(outcome=result.value, finished_at=finished_at, data=_data(attempt)))
            if result == CallbackOutcome.CONNECTED:
                state, next_at, sms = CallbackState.DONE, finished_at, False
            elif result == CallbackOutcome.CANCELLED:
                state, next_at, sms = CallbackState.CANCELLED, finished_at, False
            elif result == CallbackOutcome.UNKNOWN:
                state, next_at, sms = CallbackState.MANUAL, finished_at, False
            elif task.attempts >= 3:
                state, next_at, sms = CallbackState.EXHAUSTED, finished_at, True
            else:
                from datetime import timedelta
                state, next_at, sms = CallbackState.QUEUED, finished_at + timedelta(minutes=5), False
            updated = task.model_copy(update={"state": state, "next_at": next_at,
                "last_outcome": result, "sms_pending": sms, "owner": None,
                "lease_until": None, "version": task.version + 1})
            await self._save_task(connection, updated)
            return updated

    async def cancel_callback_task(self, task_id, owner, expected_version):
        async with journal_transaction(self.engine) as connection:
            task = await self._read_task(connection, task_id)
            if task is None or task.version != expected_version:
                raise HandoffConflict("задача уже изменилась")
            if task.owner not in {None, owner} or task.state == CallbackState.IN_PROGRESS:
                raise HandoffConflict("задача исполняется другим сотрудником")
            updated = task.model_copy(update={"state": CallbackState.CANCELLED,
                "owner": None, "lease_until": None, "version": task.version + 1})
            await self._save_task(connection, updated)
            return updated
