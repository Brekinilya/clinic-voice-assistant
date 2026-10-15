"""Возможности индексированного источника; старые расписания остаются совместимыми."""

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.booking.models import Slot
from app.booking.preferences import SlotPreferences
from app.mis.models import Client, Doctor
from app.scheduling.matching import profile_key


@runtime_checkable
class IndexedSource(Protocol):
    def find(
        self, specialty: str, preferences: SlotPreferences, now: datetime
    ) -> tuple[Slot, ...]: ...
    def contains(self, slot: Slot) -> bool: ...
    def remember_patient(self, client: Client) -> None: ...
    def matches_doctor(self, doctor: Doctor) -> bool: ...
    def specialties_for(self, specialty: str) -> tuple[str, ...]: ...


def remember(schedule, patient: Client) -> None:
    if isinstance(schedule, IndexedSource):
        schedule.remember_patient(patient)


def contains(schedule, slot: Slot) -> bool:
    return (
        schedule.contains(slot) if isinstance(schedule, IndexedSource) else slot in schedule.slots()
    )


def doctor_matches(schedule, doctor: Doctor) -> bool:
    return schedule.matches_doctor(doctor) if isinstance(schedule, IndexedSource) else True


async def available_doctors(schedule, mis, specialty: str):
    # Метка профиля из каталога учитывает регистр фильтра МИС и сокращения NLU.
    labels = (
        schedule.specialties_for(specialty) if isinstance(schedule, IndexedSource) else (specialty,)
    )
    return {doctor.id: doctor for label in labels for doctor in await mis.doctors(label)}


def candidates(schedule, specialty: str, preferences: SlotPreferences, now: datetime):
    if isinstance(schedule, IndexedSource):
        return schedule.find(specialty, preferences, now)
    return tuple(
        sorted(
            (
                s
                for s in schedule.slots()
                if profile_key(s.specialty) == profile_key(specialty)
                and now.date() <= s.scheduled_at.date() <= now.date() + timedelta(days=13)
                and preferences.matches(s)
            ),
            key=lambda s: (s.scheduled_at, s.doctor_id),
            reverse=preferences.time_pref == "late",
        )
    )
