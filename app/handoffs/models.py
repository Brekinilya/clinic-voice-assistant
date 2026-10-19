"""Долговечные факты передачи и callback. Имена статусов не смешивают исходы."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from app.booking.models import BookingModel
from app.mis.models import MisId


class TransferState(StrEnum):
    REQUESTED = "requested"
    ATTEMPTING = "attempting"
    CONNECTED = "connected"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    ABORTED = "aborted"


class MisDeliveryState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    SENDING = "sending"
    APPLIED = "applied"
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    MANUAL = "manual"


class CallbackState(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"
    MANUAL = "manual"


class CallbackOutcome(StrEnum):
    CONNECTED = "connected"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class HandoffRequest(BookingModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    call_id: str = Field(min_length=1, max_length=128)
    source: Literal["phone", "text"] = "phone"
    created_at: datetime
    route: str = Field(min_length=1, max_length=16)
    priority: Literal["P1", "P2", "P3"]
    identity_status: Literal["verified", "unknown"] = "unknown"
    client_id: MisId | None = None
    purpose: str = Field(default="unknown", max_length=32)
    summary: str = Field(default="", max_length=200)
    recording_opt_out: bool | None = None
    transfer_state: TransferState = TransferState.REQUESTED
    attempt_id: str | None = None
    transfer_reason: str | None = Field(default=None, max_length=64)
    mis_state: MisDeliveryState = MisDeliveryState.NOT_REQUIRED
    mis_operation_id: str | None = None
    mis_escalation_id: MisId | None = None
    callback_state: CallbackState | None = None
    callback_deadline: datetime | None = None
    callback_preferred_at: datetime | None = None
    callback_attempts: int = Field(default=0, ge=0, le=3)
    callback_sms_pending: bool = False
    version: int = Field(default=1, ge=1)


class TransferAttempt(BookingModel):
    attempt_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    call_id: str = Field(min_length=1, max_length=128)
    route: str | None = Field(default=None, max_length=16)
    priority: Literal["P1", "P2", "P3"]
    started_at: datetime
    finished_at: datetime | None = None
    outcome: TransferState = TransferState.ATTEMPTING
    reason: str | None = Field(default=None, max_length=64)


class MisOperation(BookingModel):
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["transfer", "emergency_notice"]
    client_id: MisId
    description: str = Field(min_length=1, max_length=1000, repr=False)
    marker: str = Field(min_length=8, max_length=48)
    state: MisDeliveryState = MisDeliveryState.PENDING
    created_at: datetime
    sending_at: datetime | None = None
    next_check_at: datetime | None = None
    checks: int = Field(default=0, ge=0, le=10)
    escalation_id: MisId | None = None
    reason: str | None = Field(default=None, max_length=64)
    version: int = Field(default=1, ge=1)

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("описание не должно быть пустым")
        if any(
            ord(char) <= 8
            or char in "\v\f"
            or 14 <= ord(char) <= 31
            or 127 <= ord(char) <= 159
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        ):
            raise ValueError("описание содержит запрещённый символ")
        return value


class CallbackDraft(BookingModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    stage: Literal["offer", "contact", "time", "confirm", "goal"]
    contact_ciphertext: str | None = Field(default=None, repr=False)
    contact_masked: str | None = None
    preferred_at: datetime | None = None
    deadline: datetime
    summary: str = Field(max_length=200)
    input_epoch: int = Field(default=0, ge=0)
    # Повтор доставки одного input_started не отзывает согласие повторно.
    started_turn_id: int = Field(default=0, ge=0)
    # Наблюдение финальной фразы отзывает согласие, не выдавая себя за input_started.
    observed_turn_id: int = Field(default=0, ge=0)
    last_turn_id: int = Field(default=0, ge=0)
    invalid_turns: int = Field(default=0, ge=0, le=3)
    version: int = Field(default=1, ge=1)


def callback_final_turn_is_next(draft: CallbackDraft, turn_id: int) -> bool:
    """Accept only the next final, or one successor that supersedes an in-flight final."""
    watermark = max(draft.last_turn_id, draft.started_turn_id)
    if turn_id <= max(watermark, draft.observed_turn_id):
        return False
    return turn_id == watermark + 1 or (
        draft.observed_turn_id == watermark + 1 and turn_id == draft.observed_turn_id + 1
    )


class CallbackTask(BookingModel):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: CallbackState
    priority: Literal["P2", "P3"]
    summary: str = Field(default="", max_length=200)
    consented_at: datetime
    deadline: datetime
    preferred_at: datetime | None = None
    contact_ciphertext: str = Field(repr=False)
    contact_masked: str
    endpoint: str | None = Field(default=None, max_length=64)
    attempts: int = Field(default=0, ge=0, le=3)
    active_attempt_id: str | None = None
    consent_turn_id: int = Field(default=0, ge=0)
    consent_input_epoch: int = Field(default=0, ge=0)
    consent_draft_version: int = Field(default=0, ge=0)
    next_at: datetime
    owner: str | None = Field(default=None, max_length=64)
    lease_until: datetime | None = None
    version: int = Field(default=1, ge=1)
    sms_pending: bool = False
    last_outcome: CallbackOutcome | None = None


class CallbackAttempt(BookingModel):
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    started_at: datetime
    finished_at: datetime | None = None
    outcome: CallbackOutcome = CallbackOutcome.UNKNOWN
    reason: str | None = Field(default=None, max_length=64)
    source: Literal["operator"] = "operator"
    owner: str = Field(min_length=1, max_length=64)
