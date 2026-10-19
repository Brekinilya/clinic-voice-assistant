"""Состояние и квитанции backend; общие контракты Ильи здесь не расширяются."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator

from app.booking.models import BookingModel, Slot
from app.booking.preferences import SlotPreferences
from app.cancellation.models import CancelProposal
from app.contracts import ExpectedAnswer
from app.dialogue.expectation import ActiveQuestion, active_question, expected_answer
from app.dialogue.outbound import ReminderResult, ReminderSession
from app.mis.models import MisId
from app.rescheduling.models import MoveProposal
from app.visits.models import VisitCursor, VisitQuery, VisitSummary

CallId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]


class Phase(StrEnum):
    READY = "ready"
    IDENTIFIED = "identified"
    OFFERED = "offered"
    HELD = "held"
    INTERRUPTED = "interrupted"
    ENDED = "ended"
    IDENTIFYING = "identifying"
    BOOKED = "booked"
    RECONCILING = "reconciling"
    HANDOFF = "handoff"
    VERIFYING_OWNER = "verifying_owner"
    VISITS_SHOWN = "visits_shown"
    CANCEL_CHOOSING = "cancel_choosing"
    CANCEL_OFFER = "cancel_offer"
    CANCEL_CONFIRM = "cancel_confirm"
    CANCEL_RECONCILING = "cancel_reconciling"
    CANCELLED = "cancelled"
    MOVE_CHOOSING = "move_choosing"
    MOVE_EMPTY = "move_empty"
    MOVE_SOURCE = "move_source"
    MOVE_OFFERED = "move_offered"
    MOVE_CONFIRM = "move_confirm"
    MOVE_FINISH_CONFIRM = "move_finish_confirm"
    MOVE_PENDING = "move_pending"
    RESCHEDULED = "rescheduled"
    REMIND_VERIFY = "remind_verify"
    REMIND_DETAILS = "remind_details"


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


class InputStart(BookingModel):
    """Начало новой речи: ещё без текста, но старое разрешение уже недействительно."""

    turn_id: int = Field(strict=True, ge=1)


class Proposal(BookingModel):
    action_id: str
    revision: int
    slot: Slot
    expires_at: AwareDatetime
    doctor_name: str | None = None


class WriteAuthorization(BookingModel):
    turn_id: int
    version: int
    input_epoch: int
    action_id: str
    revision: int


class TransferRequest(BookingModel):
    route: str
    priority: Literal["P1", "P2", "P3"]
    status: Literal["requested"] = "requested"
    action_id: str | None = None
    reason: Literal["move_create_rejected", "move_manual", "mis_wait"] | None = None


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
    # Начало непрерывного ожидания МИС в этом звонке; срок ожидания отсчитывается от него.
    mis_wait_since: AwareDatetime | None = None
    input_epoch: int = 0
    observed_turn_id: int = 0
    started_turn_id: int = 0
    observed_digest: str | None = Field(default=None, repr=False)
    authorization: WriteAuthorization | None = None
    specialty: str | None = None
    preferences: SlotPreferences = Field(default_factory=SlotPreferences)
    misunderstandings: int = 0
    end_confirmation_pending: bool = False
    shown_option_keys: tuple[str, ...] = Field(default=(), max_length=100)
    recording_opt_out: bool = False
    medical_boundary_pending: bool = False
    # Только ключ общей справки, без текста пациента; старые JSON-сессии совместимы.
    information_topic: str | None = None
    information_clarifying: bool = False
    transfer: TransferRequest | None = None
    visit_id: int | None = None
    purpose: Literal["booking", "visits", "cancel", "reschedule"] = "booking"
    move_proposal: MoveProposal | None = None
    move_target: VisitSummary | None = None
    cancel_proposal: CancelProposal | None = None
    cancel_target: VisitSummary | None = None
    cancel_reschedule_offered: bool = False
    query_for_self: bool = False
    identity_attempts: int = 0
    identity_draft: str | None = Field(default=None, repr=False)
    visit_query: VisitQuery = Field(default_factory=VisitQuery)
    visit_items: tuple[VisitSummary, ...] = Field(default=(), max_length=3)
    visit_cursor: VisitCursor | None = None
    visit_has_more: bool = False
    reminder: ReminderSession | None = Field(default=None, repr=False)
    reminder_result: ReminderResult | None = None


class InformationReference(BookingModel):
    document: str
    version: str
    topic: str
    sections: tuple[str, ...]


class Reply(BookingModel):
    call_id: CallId
    turn_id: int
    version: int
    phase: Phase
    kind: Literal["reply", "processing", "partial"] = "reply"
    utterances: tuple[str, ...]
    expect: ExpectedAnswer = "free"
    # Вычисляется из состояния; старые квитанции без поля остаются читаемыми.
    question: ActiveQuestion | None = None
    reminder_result: ReminderResult | None = None
    options: tuple[Slot, ...] = Field(default=(), max_length=3)
    proposal: Proposal | None = None
    code: str | None = None
    retry_at: AwareDatetime | None = None
    # После этого момента звонок не ждёт МИС, а передаётся регистратору.
    wait_deadline: AwareDatetime | None = None
    transfer: TransferRequest | None = None
    visit_id: int | None = None
    visits: tuple[VisitSummary, ...] = Field(default=(), max_length=3)
    has_more_visits: bool = False
    cancel_proposal: CancelProposal | None = None
    move_proposal: MoveProposal | None = None
    information: InformationReference | None = None

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
        expect=expected_answer(state),
        question=active_question(state),
        options=state.options,
        proposal=state.proposal,
        retry_at=state.retry_at,
        code=code,
        kind=kind,
        transfer=state.transfer,
        visit_id=state.visit_id,
        visits=state.visit_items,
        has_more_visits=state.visit_has_more,
        cancel_proposal=state.cancel_proposal,
        move_proposal=state.move_proposal,
        reminder_result=state.reminder_result,
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
