"""Смысл команды отделён от её формы: впоследствии сюда подключается адаптер NLU."""

from datetime import date
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from app.booking.models import BookingModel
from app.dialogue.models import DialogueState, Turn
from app.mis.models import parse_date


class Operation(StrEnum):
    HELP = "help"
    STATUS = "status"
    IDENTIFY = "identify"
    OPTIONS = "options"
    SELECT = "select"
    DECLINE = "decline"
    CONFIRM = "confirm"
    UNKNOWN = "unknown"


class PatientInput(BookingModel):
    last_name: str = Field(repr=False)
    first_name: str = Field(repr=False)
    middle_name: str | None = Field(repr=False)
    birth_date: date = Field(repr=False)


class Command(BookingModel):
    operation: Operation
    patient: PatientInput | None = Field(default=None, repr=False)
    specialty: str | None = None
    choice_index: int | None = None


class InputInterpreter(Protocol):
    async def interpret(self, state: DialogueState, turn: Turn) -> Command: ...


class CommandInterpreter:
    """Только явные команды автономного стенда. Свободные фразы не интерпретируем."""

    async def interpret(self, state: DialogueState, turn: Turn) -> Command:
        name, _, argument = turn.text.strip().partition(" ")
        if name in {"начать", "помощь"} and not argument:
            return Command(operation=Operation.HELP)
        if name == "статус" and not argument:
            return Command(operation=Operation.STATUS)
        if name == "нет" and not argument:
            return Command(operation=Operation.DECLINE)
        if name == "да":
            return Command(operation=Operation.CONFIRM)
        if name == "варианты":
            return Command(operation=Operation.OPTIONS, specialty=argument.strip() or None)
        if name == "выбрать":
            try:
                index = int(argument)
            except ValueError:
                index = None
            return Command(operation=Operation.SELECT, choice_index=index)
        if name == "пациент":
            fields = [part.strip() for part in argument.split(";")]
            try:
                if len(fields) != 4:
                    raise ValueError("формат команды")
                patient = PatientInput(
                    last_name=fields[0],
                    first_name=fields[1],
                    middle_name=fields[2] or None,
                    birth_date=parse_date(fields[3]),
                )
            except ValueError:
                patient = None
            # Намерение изменить пациента сохраняется и при ошибке, чтобы снять старую бронь.
            return Command(operation=Operation.IDENTIFY, patient=patient)
        return Command(operation=Operation.UNKNOWN)
