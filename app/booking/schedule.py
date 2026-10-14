"""Импорт явного расписания и фактов допуска, без вывода смен из истории посещений."""

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from app.booking.models import BookingModel, Slot
from app.mis.models import MisId
from app.ports import Clock


class AccessState(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"


class AccessFact(BookingModel):
    client_id: MisId = Field(repr=False)
    doctor_id: MisId
    state: AccessState
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    source: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.valid_until < self.valid_from:
            raise ValueError("границы допуска перепутаны")
        return self


class ScheduleDocument(BookingModel):
    schema_version: Literal[1]
    timezone: Literal["GMT+4"]
    source: str = Field(min_length=1, max_length=200)
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    slots: tuple[Slot, ...] = Field(max_length=20000)
    access: tuple[AccessFact, ...] = Field(default=(), max_length=20000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("версия должна быть целым числом")
        return value

    @model_validator(mode="after")
    def coherent_data(self) -> Self:
        if self.expires_at <= self.generated_at:
            raise ValueError("срок актуальности расписания должен быть положительным")
        keys = [(s.doctor_id, s.scheduled_at) for s in self.slots]
        if len(keys) != len(set(keys)):
            raise ValueError("у врача повторяется слот")
        specialties = {}
        for slot in self.slots:
            if slot.doctor_id in specialties and specialties[slot.doctor_id] != slot.specialty:
                raise ValueError("у врача противоречивые специальности")
            specialties[slot.doctor_id] = slot.specialty
        facts: dict[tuple[int, int], list[AccessFact]] = {}
        for fact in self.access:
            if fact.doctor_id not in specialties:
                raise ValueError("допуск относится к отсутствующему врачу")
            facts.setdefault((fact.client_id, fact.doctor_id), []).append(fact)
        for values in facts.values():
            ordered = sorted(values, key=lambda f: f.valid_from)
            if any(a.valid_until >= b.valid_from for a, b in zip(ordered, ordered[1:])):
                raise ValueError("периоды фактов допуска пересекаются")
        return self


class ScheduleError(Exception):
    pass


def unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("повтор поля JSON")
        result[key] = value
    return result


class JsonSchedule:
    def __init__(self, document: ScheduleDocument, clock: Clock, fingerprint: str) -> None:
        self.document = document
        self.clock = clock
        self.fingerprint = fingerprint

    @classmethod
    def load(cls, path: Path, clock: Clock) -> "JsonSchedule":
        try:
            # Читаем с ограничением и затем проверяем весь документ, прежде чем его использовать.
            with path.open("rb") as source:
                payload = source.read(4_000_001)
            if len(payload) > 4_000_000:
                raise ScheduleError("SCHEDULE_TOO_LARGE")
            document = ScheduleDocument.model_validate(
                json.loads(payload, object_pairs_hook=unique_fields)
            )
        except (OSError, ValidationError, ValueError):
            raise ScheduleError("INVALID_SCHEDULE") from None
        if not document.generated_at <= clock.now() < document.expires_at:
            raise ScheduleError("SCHEDULE_NOT_CURRENT")
        return cls(document, clock, hashlib.sha256(payload).hexdigest())

    def slots(self) -> tuple[Slot, ...]:
        if not self.document.generated_at <= self.clock.now() < self.document.expires_at:
            return ()
        return self.document.slots

    def access_state(self, client_id: int, slot: Slot) -> AccessState:
        if slot not in self.slots():
            return AccessState.UNKNOWN
        for fact in self.document.access:
            if (
                fact.client_id == client_id
                and fact.doctor_id == slot.doctor_id
                and fact.valid_from <= slot.scheduled_at <= fact.valid_until
            ):
                return fact.state
        return AccessState.UNKNOWN

    def eligible(self, client_id: int, slot: Slot) -> bool:
        return self.access_state(client_id, slot) == AccessState.ALLOWED
