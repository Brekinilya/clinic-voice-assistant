"""Отмена: свежая карточка, сохранённое подтверждение, PATCH и обязательная сверка."""

from app.booking.models import Approval, BookingError
from app.cancellation.models import (
    CancelAction,
    CancelState,
    Reason,
    check_approval,
    same_visit,
    summary,
)
from app.cancellation.ports import CancellationStore
from app.mis.budget import Priority, demand
from app.mis.models import VisitChange, VisitStatus
from app.mis.ports import BudgetExhausted, MedicalSystem, MisError, MisOutcomeUnknown
from app.ports import Clock
from app.visits.models import VisitSummary

# Свежая карточка записи, PATCH отмены и сверка результата.
CONFIRM_COST = 3


class CancellationService:
    def __init__(
        self,
        mis: MedicalSystem,
        store: CancellationStore,
        clock: Clock,
        *,
        require_dialogue_guard: bool = True,
    ) -> None:
        self.mis, self.store, self.clock = mis, store, clock
        self.require_dialogue_guard = require_dialogue_guard

    async def prepare(
        self, call_id: str, client_id: int, selected: VisitSummary, reason: Reason | None = None
    ) -> CancelAction:
        if reason == "reschedule":
            raise BookingError("RESCHEDULE_REQUIRES_WORKFLOW")
        current = await self.mis.visit(selected.visit_id)
        if current.client_id != client_id:
            raise BookingError("IDENTIFICATION_REQUIRED")
        if summary(current) != selected.model_copy(update={"room": None}):
            raise BookingError("VISIT_CHANGED")
        if current.status != VisitStatus.PLANNED:
            raise BookingError("VISIT_CHANGED")
        if current.scheduled_at <= self.clock.now():
            raise BookingError("VISIT_PAST")
        return await self.store.prepare(call_id, current, self.clock.now(), reason)

    async def confirm(self, approval: Approval) -> CancelAction:
        if self.require_dialogue_guard and (
            approval.guard_version is None or approval.input_epoch is None
        ):
            raise BookingError("DIALOGUE_GUARD_REQUIRED")
        action = await self.store.get(approval.action_id)
        check_approval(action, approval)
        if action.state != CancelState.PREPARED:
            action, _ = await self.store.claim(approval, self.clock.now())
            return action
        # Свежая карточка и PATCH идут подряд по заранее занятым местам окна.
        with demand(Priority.COMMIT):
            async with self.mis.prepaid(CONFIRM_COST, keep_for_write=1):
                action, sent = await self._commit(action, approval)
                # Сверка сразу после PATCH — по оставшемуся месту, без ожидания окна.
                return await self.reconcile(action.action_id, action.call_id) if sent else action

    async def _commit(self, action: CancelAction, approval: Approval) -> tuple[CancelAction, bool]:
        current = await self.mis.visit(action.before.id)
        if (
            not same_visit(action.before, current)
            or current.status not in {VisitStatus.PLANNED, VisitStatus.CANCELLED}
            or (
                current.status == VisitStatus.PLANNED
                and current.updated_at != action.before.updated_at
            )
        ):
            raise BookingError("VISIT_CHANGED")
        action, claimed = await self.store.claim(approval, self.clock.now())
        if not claimed:
            return action, False
        if current.status == VisitStatus.CANCELLED:
            # Чужая подтверждённая отмена уже достигла цели; повторного PATCH нет.
            return await self.store.finish(
                action.action_id, CancelState.CANCELLED, self.clock.now()
            ), False
        try:
            result = await self.mis.change_visit(
                action.before.id, VisitChange(status=VisitStatus.CANCELLED)
            )
        except BudgetExhausted:
            await self.store.restore_unsent(action.action_id, approval.turn_id)
            raise
        except MisOutcomeUnknown:
            return await self.store.finish(
                action.action_id, CancelState.RECONCILING, self.clock.now()
            ), False
        except MisError:
            return await self.store.finish(
                action.action_id, CancelState.REJECTED, self.clock.now()
            ), False
        if not same_visit(action.before, result) or result.status != VisitStatus.CANCELLED:
            return await self.store.finish(
                action.action_id, CancelState.RECONCILING, self.clock.now()
            ), False
        return action, True

    async def reconcile(self, action_id: str, call_id: str) -> CancelAction:
        action = await self.store.get(action_id)
        if action.call_id != call_id:
            raise BookingError("CONFIRMATION_REQUIRED")
        if action.state == CancelState.CANCELLED:
            return action
        if action.state not in {CancelState.COMMITTING, CancelState.RECONCILING}:
            raise BookingError("RECONCILIATION_NOT_REQUIRED")
        try:
            with demand(Priority.RECONCILE):
                current = await self.mis.visit(action.before.id)
        except MisError:
            return await self.store.finish(action_id, CancelState.RECONCILING, self.clock.now())
        if same_visit(action.before, current) and current.status == VisitStatus.CANCELLED:
            return await self.store.finish(action_id, CancelState.CANCELLED, self.clock.now())
        # Пустой, изменённый или ещё запланированный результат не разрешает повтор PATCH.
        return await self.store.finish(action_id, CancelState.RECONCILING, self.clock.now())
