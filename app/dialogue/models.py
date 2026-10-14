"""Состояние и квитанции backend; общие контракты Ильи здесь не расширяются."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator

from app.booking.models import BookingModel, Slot
from app.mis.models import MisId

CallId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class Phase(StrEnum):
    READY = "ready"
    IDENTIFIED = "identified"
    OFFERED = "offered"
    HELD = "held"
    INTERRUPTED = "interrupted"
    ENDED = "ended"


class Turn(BookingModel):
    turn_id: int = Field(strict=True, ge=1)
    text: str = Field(min_length=1, max_length=4000, repr=False)
    is_final: StrictBool = True

    @field_validator("text")
    @classmethod
    def nonempty(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("нужен текст команды")
        return text


class Proposal(BookingModel):
    action_id: str
    revision: int
    slot: Slot
    expires_at: AwareDatetime


class DialogueState(BookingModel):
    call_id: CallId
    version: int = 0
    last_turn_id: int = 0
    pending_turn_id: int | None = None
    phase: Phase = Phase.READY
    client_id: MisId | None = Field(default=None, repr=False)
    options: tuple[Slot, ...] = Field(default=(), max_length=3)
    proposal: Proposal | None = None
    retry_at: AwareDatetime | None = None


class Reply(BookingModel):
    call_id: CallId
    turn_id: int
    version: int
    phase: Phase
    kind: Literal["reply", "processing", "partial"] = "reply"
    utterances: tuple[str, ...]
    options: tuple[Slot, ...] = Field(default=(), max_length=3)
    proposal: Proposal | None = None
    code: str | None = None
    retry_at: AwareDatetime | None = None

    @field_validator("utterances")
    @classmethod
    def short_phrases(cls, phrases: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(text.split()) > 20 for text in phrases):
            raise ValueError("фраза ассистента длиннее 20 слов")
        return phrases


def reply(
    state: DialogueState,
    turn_id: int,
    *phrases: str,
    code: str | None = None,
    kind: Literal["reply", "processing", "partial"] = "reply",
) -> Reply:
    return Reply(
        call_id=state.call_id,
        turn_id=turn_id,
        version=state.version,
        phase=state.phase,
        utterances=phrases,
        options=state.options,
        proposal=state.proposal,
        retry_at=state.retry_at,
        code=code,
        kind=kind,
    )


class Receipt(BookingModel):
    digest: str = Field(repr=False)
    reply: Reply | None = None


class Claim(BookingModel):
    state: DialogueState
    acquired: bool
    cached: Reply | None = None


class DialogueError(Exception):
    pass
