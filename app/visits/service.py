"""Чтение будущих записей конкретного пациента, без изменения МИС и кэша посещений."""

from app.clock import CLINIC_TIMEZONE
from app.mis.models import VisitStatus
from app.mis.ports import MedicalSystem, MisUnavailable
from app.ports import Clock
from app.visits.models import VisitCursor, VisitPage, VisitQuery, VisitSummary
from app.visits.ports import DoctorRooms


class VisitService:
    def __init__(self, mis: MedicalSystem, clock: Clock, rooms: DoctorRooms | None = None) -> None:
        self.mis, self.clock, self.rooms = mis, clock, rooms

    async def page(
        self,
        client_id: int,
        query: VisitQuery,
        *,
        after: VisitCursor | None = None,
    ) -> VisitPage:
        patient = await self.mis.client(client_id)
        if patient.id != client_id:
            raise MisUnavailable("INVALID_RESPONSE")
        now = self.clock.now().astimezone(CLINIC_TIMEZONE)
        visits = await self.mis.visits(
            client_id=client_id, status=VisitStatus.PLANNED, date_from=now.date()
        )
        if any(v.client_id != client_id for v in visits):
            # Повреждённый/неверно отфильтрованный ответ не превращаем в чужую справку.
            raise MisUnavailable("INVALID_RESPONSE")
        matching = sorted(
            (
                v
                for v in visits
                if v.status == VisitStatus.PLANNED
                and v.scheduled_at > now
                and query.matches(v)
                and (after is None or (v.scheduled_at, v.id) > after.key())
            ),
            key=lambda v: (v.scheduled_at, v.id),
        )
        items = tuple(
            VisitSummary(
                visit_id=v.id,
                doctor_id=v.doctor.id,
                doctor_name=v.doctor.full_name,
                specialty=v.doctor.specialty,
                scheduled_at=v.scheduled_at,
                room=self.rooms.room(v.doctor.id, v.scheduled_at)
                if self.rooms is not None
                else None,
            )
            for v in matching[:3]
        )
        return VisitPage(
            items=items,
            has_more=len(matching) > 3,
            cursor=(
                VisitCursor(scheduled_at=items[-1].scheduled_at, visit_id=items[-1].visit_id)
                if items
                else after
            ),
        )
