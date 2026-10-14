"""Создание записи: журнал перед POST, проверка результата и сверка вместо повтора."""

from datetime import date, timedelta

from app.booking.models import (
    ActionState,
    Approval,
    BookingAction,
    BookingError,
    Slot,
    check_approval,
)
from app.booking.ports import BookingStore, Schedule
from app.clock import CLINIC_TIMEZONE
from app.mis.models import Client, Visit, VisitCreate, VisitStatus, normalize_name
from app.mis.ports import MedicalSystem, MisError, MisOutcomeUnknown
from app.ports import Clock


class BookingService:
    def __init__(
        self, mis: MedicalSystem, store: BookingStore, schedule: Schedule, clock: Clock
    ) -> None:
        self.mis = mis
        self.store = store
        self.schedule = schedule
        self.clock = clock

    async def identify(
        self, last_name: str, first_name: str, middle_name: str | None, birth_date: date
    ) -> Client:
        candidates = await self.mis.clients(last_name, first_name, birth_date)
        expected_last, expected_first = normalize_name(last_name), normalize_name(first_name)
        expected_middle = normalize_name(middle_name) if middle_name else None
        candidates = [
            c
            for c in candidates
            if c.last_name == expected_last
            and c.first_name == expected_first
            and c.birth_date == birth_date
            and c.middle_name == expected_middle
        ]
        if len(candidates) != 1:
            # Никакого автоматического создания пациента и угадывания похожей фамилии.
            raise BookingError("IDENTIFICATION_REQUIRED")
        return candidates[0]

    def _check_slot(self, client_id: int, slot: Slot) -> None:
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        if slot not in self.schedule.slots() or not self.schedule.eligible(client_id, slot):
            raise BookingError("ELIGIBILITY_UNKNOWN")
        # Четырнадцать календарных дней: сегодняшний день входит в горизонт.
        if not now.date() <= slot.scheduled_at.date() <= now.date() + timedelta(days=13):
            raise BookingError("OUTSIDE_HORIZON")
        if slot.scheduled_at < now + timedelta(minutes=30):
            raise BookingError("TOO_SOON")

    async def options(self, client_id: int, specialty: str) -> tuple[Slot, ...]:
        await self.mis.client(client_id)
        doctors = {d.id: d for d in await self.mis.doctors(specialty)}
        visits = await self.mis.visits(
            status=VisitStatus.PLANNED,
            date_from=self.clock.now().astimezone(CLINIC_TIMEZONE).date(),
        )
        if any(v.client_id == client_id and v.doctor.specialty == specialty for v in visits):
            raise BookingError("SPECIALTY_ALREADY_BOOKED")
        busy = {(v.doctor.id, v.scheduled_at) for v in visits}
        result = []
        for slot in sorted(self.schedule.slots(), key=lambda s: (s.scheduled_at, s.doctor_id)):
            if (
                slot.specialty != specialty
                or slot.doctor_id not in doctors
                or not doctors[slot.doctor_id].is_active
                or doctors[slot.doctor_id].specialty != slot.specialty
            ):
                continue
            try:
                self._check_slot(client_id, slot)
            except BookingError:
                continue
            if (slot.doctor_id, slot.scheduled_at) not in busy and await self.store.available(
                slot, self.clock.now()
            ):
                result.append(slot)
                if len(result) == 3:
                    break
        return tuple(result)

    async def propose(self, call_id: str, client_id: int, slot: Slot) -> BookingAction:
        self._check_slot(client_id, slot)
        await self._preflight(client_id, slot)
        return await self.store.hold(call_id, client_id, slot, self.clock.now())

    async def _preflight(self, client_id: int, slot: Slot, *, fresh: bool = False) -> None:
        await self.mis.client(client_id)
        doctors = {d.id: d for d in await self.mis.doctors(slot.specialty, fresh=fresh)}
        doctor = doctors.get(slot.doctor_id)
        if doctor is None or not doctor.is_active or doctor.specialty != slot.specialty:
            raise BookingError("DOCTOR_UNAVAILABLE")
        visits = await self.mis.visits(
            status=VisitStatus.PLANNED,
            date_from=self.clock.now().astimezone(CLINIC_TIMEZONE).date(),
        )
        if any(
            v.doctor.id == slot.doctor_id and v.scheduled_at == slot.scheduled_at for v in visits
        ):
            raise BookingError("SLOT_BUSY")
        if any(v.client_id == client_id and v.doctor.specialty == slot.specialty for v in visits):
            raise BookingError("SPECIALTY_ALREADY_BOOKED")

    async def confirm(self, approval: Approval) -> BookingAction:
        action = await self.store.get(approval.action_id)
        check_approval(action, approval)
        if action.state != ActionState.HELD:
            # Повтор того же подтверждения возвращает журнал, а не новый запрос.
            action, _ = await self.store.claim(approval, self.clock.now())
            return action
        self._check_slot(action.client_id, action.slot)
        try:
            await self._preflight(action.client_id, action.slot, fresh=True)
        except (MisError, BookingError):
            latest = await self.store.get(action.action_id)
            if latest.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }:
                check_approval(latest, approval)
                return latest
            raise
        action, claimed = await self.store.claim(approval, self.clock.now())
        if not claimed:
            return action
        request = VisitCreate(
            client_id=action.client_id,
            doctor_id=action.slot.doctor_id,
            scheduled_at=action.slot.scheduled_at,
        )
        try:
            visit = await self.mis.create_visit(request)
        except MisOutcomeUnknown:
            return await self.store.finish(action.action_id, ActionState.RECONCILING)
        except MisError:
            return await self.store.finish(action.action_id, ActionState.REJECTED)
        # Неожиданный сбой/отмена задачи оставляет COMMITTING в сохранённом журнале.
        # Такой исход тоже требует сверки, даже если HTTP ещё не успел отправиться.
        if not self._matches(action, visit):
            return await self.store.finish(action.action_id, ActionState.RECONCILING)
        return await self.store.finish(action.action_id, ActionState.BOOKED, visit.id)

    @staticmethod
    def _matches(action: BookingAction, visit: Visit) -> bool:
        return (
            visit.client_id == action.client_id
            and visit.doctor.id == action.slot.doctor_id
            and visit.doctor.specialty == action.slot.specialty
            and visit.scheduled_at == action.slot.scheduled_at
            and visit.status == VisitStatus.PLANNED
        )

    async def reconcile(self, action_id: str) -> BookingAction:
        action = await self.store.get(action_id)
        if action.state in {ActionState.BOOKED, ActionState.ARCHIVED}:
            return action
        if action.state not in {ActionState.COMMITTING, ActionState.RECONCILING}:
            raise BookingError("RECONCILIATION_NOT_REQUIRED")
        # Сверку COMMITTING запускает оператор только после остановки прежнего worker.
        # Пустой результат не доказывает отсутствие записи: POST мог ещё выполняться.
        visits = await self.mis.visits(
            client_id=action.client_id,
            doctor_id=action.slot.doctor_id,
            date_from=action.slot.scheduled_at.date(),
            date_to=action.slot.scheduled_at.date(),
        )
        matches = [v for v in visits if self._matches(action, v)]
        if len(matches) == 1:
            return await self.store.finish(action_id, ActionState.BOOKED, matches[0].id)
        return await self.store.finish(action_id, ActionState.RECONCILING)
