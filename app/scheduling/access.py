"""Возраст из проверенной карточки и явные факты допуска; регламент 2.3."""

from datetime import date, datetime

from app.booking.schedule import AccessFact, AccessState
from app.scheduling.matching import profile_key

PATIENT_GROUPS = {
    "Взрослые 18+": (18, None),
    "Дети 0–17 лет": (0, 17),
    "Дети до 1 года": (0, 0),
    "Женщины 16+": (16, None),
    "Дети и взрослые": (0, None),
}
# Остальные профили требуют явного подтверждённого допуска (участок/направление).
DIRECT = {
    "хирург",
    "офтальмолог",
    "лор",
    "детский хирург",
    "детский офтальмолог",
    "детский лор",
    "стоматолог-терапевт",
    "стоматолог-хирург",
    "акушер-гинеколог",
}
PAID = {"стоматолог-ортопед", "ортодонт"}


def access_state(
    birth_date: date | None,
    doctor_id: int,
    specialty: str,
    group: str,
    at: datetime,
    client_id: int,
    facts: tuple[AccessFact, ...],
) -> AccessState:
    day = at.date()
    if birth_date is None or birth_date > day:
        return AccessState.UNKNOWN
    age = day.year - birth_date.year - ((day.month, day.day) < (birth_date.month, birth_date.day))
    first, last = PATIENT_GROUPS[group]
    if age < first or last is not None and age > last:
        return AccessState.DENIED
    profile = profile_key(specialty)
    if profile in PAID:
        return AccessState.UNKNOWN
    matching = [
        f
        for f in facts
        if f.client_id == client_id
        and f.doctor_id == doctor_id
        and f.valid_from <= at <= f.valid_until
    ]
    if matching:
        return matching[0].state
    if group == "Женщины 16+" or profile not in DIRECT:
        return AccessState.UNKNOWN
    return AccessState.ALLOWED
