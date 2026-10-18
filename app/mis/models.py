"""Строгие модели подмножества OpenAPI, необходимого для записи на приём."""

import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
)

from app.clock import CLINIC_TIMEZONE


def parse_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value.strip()
    ):
        raise ValueError("дата должна иметь формат ДД.ММ.ГГГГ")
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("нужен часовой пояс")
        result = value.astimezone(CLINIC_TIMEZONE)
    else:
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{2}\.[0-9]{2}\.[0-9]{4} [0-9]{2}:[0-9]{2}", value.strip()
        ):
            raise ValueError("момент должен иметь формат ДД.ММ.ГГГГ ЧЧ:ММ")
        result = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=CLINIC_TIMEZONE)
    if result.second or result.microsecond:
        raise ValueError("МИС принимает только целые минуты")
    return result


def format_date(value: date) -> str:
    return f"{value.day:02}.{value.month:02}.{value.year:04}"


def format_datetime(value: datetime) -> str:
    value = parse_datetime(value)
    return f"{format_date(value.date())} {value.hour:02}:{value.minute:02}"


def normalize_name(value: str) -> str:
    if any(ord(c) <= 8 or 14 <= ord(c) <= 31 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("имя содержит управляющие символы")
    result = " ".join(unicodedata.normalize("NFC", value).split())
    if not result or len(result) > 100:
        raise ValueError("имя должно содержать от 1 до 100 символов")
    return result


def name_key(value: str) -> str:
    # Только регистр и е/ё; опечатки и похожие фамилии не объединяем.
    return normalize_name(value).casefold().replace("ё", "е")


MisId = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
MisDate = Annotated[
    date, BeforeValidator(parse_date), PlainSerializer(format_date, when_used="json")
]
MisDateTime = Annotated[
    datetime, BeforeValidator(parse_datetime), PlainSerializer(format_datetime, when_used="json")
]
Name = Annotated[str, Field(strict=True), AfterValidator(normalize_name)]


class MisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Doctor(MisModel):
    id: MisId
    full_name: str
    specialty: str
    is_active: StrictBool


class Client(MisModel):
    id: MisId
    last_name: Name = Field(repr=False)
    first_name: Name = Field(repr=False)
    middle_name: str | None = Field(repr=False)
    birth_date: MisDate = Field(repr=False)
    comment: str | None = Field(repr=False)
    created_at: MisDateTime


class VisitStatus(StrEnum):
    PLANNED = "Запланирована"
    CANCELLED = "Отменена"
    COMPLETED = "Завершена"


class Visit(MisModel):
    id: MisId
    client_id: MisId = Field(repr=False)
    doctor: Doctor
    scheduled_at: MisDateTime
    status: VisitStatus
    created_at: MisDateTime
    updated_at: MisDateTime


class VisitCreate(MisModel):
    client_id: MisId = Field(repr=False)
    doctor_id: MisId
    scheduled_at: MisDateTime


COMMENT_LIMIT = 1000


class ClientComment(MisModel):
    """Тело PATCH /clients/{id}: комментарий заменяется целиком, null его стирает."""

    comment: str = Field(max_length=COMMENT_LIMIT, repr=False)

    def body(self) -> dict:
        if any(
            ord(c) <= 8 or c in "" or 14 <= ord(c) <= 31 or 127 <= ord(c) <= 159
            for c in self.comment
        ):
            raise ValueError("комментарий содержит управляющие символы")
        return {"comment": self.comment}


class VisitChange(MisModel):
    doctor_id: MisId | None = None
    scheduled_at: MisDateTime | None = None
    status: VisitStatus | None = None

    def body(self) -> dict:
        # Отсутствующее поле и явный null в этом API имеют разный смысл.
        body = self.model_dump(mode="json", exclude_unset=True)
        if not body or any(v is None for v in body.values()):
            raise ValueError("нужно хотя бы одно изменение без null")
        return body
