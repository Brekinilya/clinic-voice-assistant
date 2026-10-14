"""Каталог МИС сохраняет происхождение; коды Excel не заменяют ID стенда."""

from datetime import date
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, model_validator

from app.booking.models import BookingError, BookingModel
from app.booking.schedule import AccessFact
from app.mis.models import Doctor


class Catalog(BookingModel):
    schema_version: Literal[1] = 1
    mode: Literal["offline_test", "hackathon"]
    base_url: str
    token_scope: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    captured_at: AwareDatetime
    doctors: tuple[Doctor, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len({d.id for d in self.doctors}) != len(self.doctors):
            raise ValueError("ID МИС повторяется")
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("нужен HTTPS-адрес без секретов и параметров")
        if self.mode == "hackathon" and not self.token_scope:
            raise ValueError("для каталога стенда нужны адрес и область токена")
        if self.mode == "offline_test" and self.token_scope is not None:
            raise ValueError("учебный каталог не относится к токену стенда")
        return self


class WorkbookDoctor(BookingModel):
    excel_id: str
    full_name: str
    specialty: str
    department: str = ""
    patient_group: str
    room: str
    minutes: int = Field(strict=True, ge=1, le=240)


class AccessRules(BookingModel):
    schema_version: Literal[1] = 1
    facts: tuple[AccessFact, ...] = Field(max_length=20000)


class SlotStatus(StrEnum):
    FREE = "Свободен"
    BOOKED = "Записан"
    COMPLETED = "Приём состоялся"
    NO_SHOW = "Неявка"
    CANCELLED = "Отменён пациентом"


class ImportReport(BookingModel):
    workbook_sha256: str
    catalog_sha256: str
    mode: Literal["offline_test", "hackathon"]
    doctors: int
    slots: int
    free_slots: int
    date_from: date
    date_to: date


class ScheduleImportError(BookingError):
    """Причина ошибки без персональных сведений и секретов."""
