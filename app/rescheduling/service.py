"""С-03: POST новой записи, свежая проверка, PATCH прежней и сверка обеих."""

from datetime import timedelta

from app.booking.models import Approval, BookingError, Slot
from app.booking.preferences import SlotPreferences
from app.booking.service import BookingService, upcoming_days
from app.cancellation.models import same_visit, summary
from app.mis.budget import Priority, demand
from app.mis.models import VisitChange, VisitCreate, VisitStatus
from app.mis.ports import BudgetExhausted, MisError, MisOutcomeUnknown
from app.recovery.coordinator import coordinate
from app.rescheduling.models import MoveState, check_approval, matches_new
from app.scheduling.matching import profile_key
from app.scheduling.query import IndexedSource, candidates, doctor_matches, remember
from app.visits.models import VisitSummary

# Создание: прежняя запись, врачи, день врача, записи профиля и POST; карточка пациента —
# ещё один запрос для индекса расписания, если её нет в кэше.
CREATE_COST = 5
# Отмена прежней: новая и прежняя записи, записи профиля, PATCH и сверка обеих записей.
CANCEL_COST = 6


class RescheduleService:
    def __init__(self, booking: BookingService, store):
        self.booking, self.store = booking, store
        self.mis, self.clock = booking.mis, booking.clock
        self.operations = booking.operations

    async def _source(self, before, *, permit_cancelled=False):
        current = await self.mis.visit(before.id)
        if (
            not same_visit(before, current)
            or current.status
            not in (
                {VisitStatus.PLANNED, VisitStatus.CANCELLED}
                if permit_cancelled
                else {VisitStatus.PLANNED}
            )
            or current.status == VisitStatus.PLANNED
            and current.updated_at != before.updated_at
        ):
            raise BookingError("VISIT_CHANGED")
        return current

    async def _others(self, before, *, call_id=None, fresh=False):
        visits = await self.mis.visits(
            client_id=before.client_id,
            status=VisitStatus.PLANNED,
            date_from=self.clock.now().date(),
            call_id=call_id,
            fresh=fresh,
        )
        return [
            v
            for v in visits
            if v.id != before.id
            and profile_key(v.doctor.specialty) == profile_key(before.doctor.specialty)
            and v.scheduled_at >= self.clock.now()
        ]

    async def options(
        self, client_id: int, selected: VisitSummary, preferences=None, *, call_id: str
    ):
        before = await self.mis.visit(selected.visit_id)
        if before.client_id != client_id or summary(before) != selected.model_copy(
            update={"room": None}
        ):
            raise BookingError("VISIT_CHANGED")
        if before.status != VisitStatus.PLANNED or before.scheduled_at <= self.clock.now():
            raise BookingError("VISIT_PAST")
        if await self._others(before, call_id=call_id):
            raise BookingError("SPECIALTY_ALREADY_BOOKED")
        preferences = preferences or SlotPreferences()
        if isinstance(self.booking.schedule, IndexedSource):
            remember(self.booking.schedule, await self.mis.client(client_id))
        doctors = {d.id: d for d in await self.mis.doctors(before.doctor.specialty)}
        slots = []
        for slot in candidates(
            self.booking.schedule, before.doctor.specialty, preferences, self.clock.now()
        ):
            doctor = doctors.get(slot.doctor_id)
            if (
                doctor is None
                or not doctor.is_active
                or doctor.specialty != slot.specialty
                or profile_key(slot.specialty) != profile_key(before.doctor.specialty)
                or not doctor_matches(self.booking.schedule, doctor)
                or not preferences.matches(slot)
                or (slot.doctor_id, slot.scheduled_at) == (before.doctor.id, before.scheduled_at)
            ):
                continue
            try:
                self.booking.validate_slot(client_id, slot)
            except BookingError:
                continue
            slots.append(slot)
        slots.sort(
            key=lambda s: (
                s.doctor_id != before.doctor.id,
                -s.scheduled_at.timestamp()
                if preferences.time_pref == "late"
                else s.scheduled_at.timestamp(),
                s.doctor_id,
            )
        )
        busy, checked, result = set(), set(), []
        for index, slot in enumerate(slots):
            if (slot.doctor_id, slot.scheduled_at.date()) not in checked:
                # Как при записи: занятость только за дни ближайших нужных вариантов.
                days = upcoming_days(slots[index:], slot.doctor_id, 3)
                busy.update(
                    (v.doctor.id, v.scheduled_at)
                    for v in await self.mis.visits(
                        doctor_id=slot.doctor_id,
                        status=VisitStatus.PLANNED,
                        date_from=min(days),
                        date_to=max(days),
                        call_id=call_id,
                    )
                )
                checked.update(
                    (slot.doctor_id, min(days) + timedelta(days=offset))
                    for offset in range((max(days) - min(days)).days + 1)
                )
            if (
                slot.doctor_id,
                slot.scheduled_at,
            ) not in busy and await self.booking.store.available(slot, self.clock.now()):
                result.append(slot)
                if len(result) == 3:
                    break
        return tuple(result)

    async def _preflight(self, before, slot, call_id, *, fresh):
        if isinstance(self.booking.schedule, IndexedSource):
            remember(self.booking.schedule, await self.mis.client(before.client_id))
        self.booking.validate_slot(before.client_id, slot)
        if profile_key(slot.specialty) != profile_key(before.doctor.specialty):
            raise BookingError("WRONG_SPECIALTY")
        doctors = {d.id: d for d in await self.mis.doctors(slot.specialty, fresh=fresh)}
        doctor = doctors.get(slot.doctor_id)
        if (
            doctor is None
            or not doctor.is_active
            or doctor.specialty != slot.specialty
            or not doctor_matches(self.booking.schedule, doctor)
        ):
            raise BookingError("DOCTOR_UNAVAILABLE")
        busy = await self.mis.visits(
            doctor_id=slot.doctor_id,
            status=VisitStatus.PLANNED,
            date_from=slot.scheduled_at.date(),
            date_to=slot.scheduled_at.date(),
            call_id=call_id,
            fresh=fresh,
        )
        if any(v.scheduled_at == slot.scheduled_at for v in busy):
            raise BookingError("SLOT_BUSY")
        if await self._others(before, call_id=call_id, fresh=fresh):
            raise BookingError("SPECIALTY_ALREADY_BOOKED")
        return doctor

    async def prepare(
        self, call_id: str, client_id: int, selected: VisitSummary, slot: Slot, room=None
    ):
        before = await self.mis.visit(selected.visit_id)
        if before.client_id != client_id or summary(before) != selected.model_copy(
            update={"room": None}
        ):
            raise BookingError("VISIT_CHANGED")
        if before.status != VisitStatus.PLANNED or before.scheduled_at <= self.clock.now():
            raise BookingError("VISIT_PAST")
        doctor = await self._preflight(before, slot, call_id, fresh=False)
        return await self.store.prepare(
            call_id, before, slot, self.clock.now(), room, doctor.full_name
        )

    async def confirm(self, approval: Approval):
        return await coordinate(
            self.operations, "move", approval.action_id, lambda: self._confirm(approval)
        )

    async def _confirm(self, approval: Approval):
        if approval.guard_version is None or approval.input_epoch is None:
            raise BookingError("DIALOGUE_GUARD_REQUIRED")
        action = await self.store.get(approval.action_id)
        check_approval(action, approval)
        # Каждый шаг — свежая проверка и запись по заранее занятым местам окна.
        with demand(Priority.COMMIT):
            if action.state == MoveState.HELD:
                cost = CREATE_COST + (
                    isinstance(self.booking.schedule, IndexedSource)
                    and not self.mis.has_client(action.before.client_id)
                )
                async with self.mis.prepaid(cost, keep_for_write=1):
                    action, proceed = await self._create(action, approval)
                if not proceed:
                    return action
            if action.state != MoveState.NEW_CREATED:
                return action
            async with self.mis.prepaid(CANCEL_COST, keep_for_write=1):
                action, sent = await self._cancel_old(action, approval)
                return await self.reconcile(action.action_id, action.call_id) if sent else action

    async def _create(self, action, approval: Approval):
        await self._source(action.before)
        await self._preflight(action.before, action.slot, action.call_id, fresh=True)
        action, claimed = await self.store.claim(approval, self.clock.now())
        if not claimed:
            return action, False
        try:
            visit = await self.mis.create_visit(
                VisitCreate(
                    client_id=action.before.client_id,
                    doctor_id=action.slot.doctor_id,
                    scheduled_at=action.slot.scheduled_at,
                )
            )
        except BudgetExhausted:
            await self.store.restore_unsent(action.action_id, approval.turn_id)
            raise
        except MisOutcomeUnknown:
            return await self.store.mark(action.action_id, MoveState.CREATE_UNKNOWN), False
        except MisError:
            return await self.store.mark(action.action_id, MoveState.REJECTED), False
        if not matches_new(action, visit):
            return await self.store.mark(action.action_id, MoveState.CREATE_UNKNOWN), False
        return await self.store.created(action.action_id, visit), True

    async def _cancel_old(self, action, approval: Approval):
        new = await self.mis.visit(action.new_visit.id)
        if not same_visit(action.new_visit, new) or not matches_new(action, new):
            return await self.store.mark(action.action_id, MoveState.MANUAL), False
        try:
            old = await self._source(action.before, permit_cancelled=True)
        except BookingError:
            return await self.store.mark(action.action_id, MoveState.MANUAL), False
        others = await self._others(action.before, fresh=True)
        if len(others) != 1 or others[0].id != new.id:
            return await self.store.mark(action.action_id, MoveState.MANUAL), False
        action, claimed = await self.store.claim(approval, self.clock.now(), cancelling=True)
        if not claimed:
            return action, False
        if old.status != VisitStatus.CANCELLED:
            try:
                result = await self.mis.change_visit(
                    old.id, VisitChange(status=VisitStatus.CANCELLED)
                )
            except BudgetExhausted:
                await self.store.restore_unsent(action.action_id, approval.turn_id, cancelling=True)
                raise
            except MisOutcomeUnknown:
                return await self.store.mark(action.action_id, MoveState.CANCEL_UNKNOWN), False
            except MisError:
                return await self.store.mark(action.action_id, MoveState.MANUAL), False
            if not same_visit(action.before, result) or result.status != VisitStatus.CANCELLED:
                return await self.store.mark(action.action_id, MoveState.CANCEL_UNKNOWN), False
        return action, True

    async def reconcile(self, action_id: str, call_id: str):
        return await coordinate(
            self.operations, "move", action_id, lambda: self._owned_reconcile(action_id, call_id)
        )

    async def _owned_reconcile(self, action_id: str, call_id: str):
        action = await self.store.get(action_id)
        if action.call_id != call_id:
            raise BookingError("CONFIRMATION_REQUIRED")
        if action.state == MoveState.RESCHEDULED:
            return action
        with demand(Priority.RECONCILE):
            return await self._reconcile(action)

    async def _reconcile(self, action):
        action_id = action.action_id
        try:
            if action.state in {MoveState.CREATING, MoveState.CREATE_UNKNOWN}:
                visits = await self.mis.visits(
                    client_id=action.before.client_id,
                    doctor_id=action.slot.doctor_id,
                    date_from=action.slot.scheduled_at.date(),
                    date_to=action.slot.scheduled_at.date(),
                    fresh=True,
                )
                found = [v for v in visits if matches_new(action, v)]
                if len(found) == 1:
                    return await self.store.created(action_id, found[0])
                return action
            if action.new_visit is None:
                raise BookingError("RECONCILIATION_NOT_REQUIRED")
            old, new = (
                await self.mis.visit(action.before.id),
                await self.mis.visit(action.new_visit.id),
            )
        except MisError:
            if action.state == MoveState.CANCELLING:
                return await self.store.mark(action_id, MoveState.CANCEL_UNKNOWN)
            return action
        if (
            same_visit(action.before, old)
            and old.status == VisitStatus.CANCELLED
            and same_visit(action.new_visit, new)
            and matches_new(action, new)
        ):
            return await self.store.complete(action_id, old, new, self.clock.now())
        if action.state == MoveState.CANCELLING:
            return await self.store.mark(action_id, MoveState.CANCEL_UNKNOWN)
        return action
