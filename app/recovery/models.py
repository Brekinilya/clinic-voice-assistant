"""В журнале восстановления только технические ссылки, без ФИО и текста разговора."""

from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from app.booking.models import BookingError, BookingModel

Kind = Literal["booking", "cancellation", "move"]
Status = Literal["pending", "running", "watching", "manual", "resolved"]


class OwnershipLost(BookingError):
    def __init__(self):
        super().__init__("CONFIRMATION_INTERRUPTED")


class RecoveryRecord(BookingModel):
    kind: Kind
    action_id: str = Field(min_length=1, max_length=128)
    state: Status = "pending"
    due_at: AwareDatetime
    fence: int = Field(default=0, ge=0)
    token: str | None = Field(default=None, repr=False)
    lease_until: AwareDatetime | None = None
    attempts: int = Field(default=0, ge=0)
    uncertain_since: AwareDatetime | None = None
    reason: str | None = None


class Lease(BookingModel):
    kind: Kind
    action_id: str
    fence: int
    token: str = Field(repr=False)
    read_only: bool = False
    watching: bool = False
    uncertain_since: AwareDatetime | None = None


class Outcome(BookingModel):
    state: Literal["pending", "watching", "manual", "resolved"]
    reason: str | None = None
    delay_s: float = Field(default=5, ge=0, le=86400)
    count_attempt: bool = False


def owned(record: RecoveryRecord, lease: Lease, now: datetime) -> None:
    now = now() if callable(now) else now
    if (
        record.state != "running"
        or record.fence != lease.fence
        or record.token != lease.token
        or record.lease_until is None
        or record.lease_until <= now
    ):
        raise OwnershipLost()


def claim(record: RecoveryRecord, now: datetime, seconds: int, *, read_only=False):
    if not 1 <= seconds <= 300:
        raise ValueError("недопустимый срок аренды")
    if record.state == "running" and record.lease_until is not None and record.lease_until > now:
        return None
    until = now + timedelta(seconds=seconds)
    updated = record.model_copy(
        update={
            "state": "running",
            "fence": record.fence + 1,
            "token": uuid4().hex,
            "lease_until": until,
            "due_at": until,
        }
    )
    return updated, Lease(
        kind=updated.kind,
        action_id=updated.action_id,
        fence=updated.fence,
        token=updated.token,
        read_only=read_only,
        watching=record.state == "watching",
        uncertain_since=record.uncertain_since,
    )


def settle(record, lease, now, outcome, *, max_attempts=3, max_age_s=600):
    now = now() if callable(now) else now
    owned(record, lease, now)
    attempts = record.attempts + int(outcome.count_attempt)
    since = record.uncertain_since or now
    state, reason = outcome.state, outcome.reason
    if state == "pending" and (
        attempts >= max_attempts or now >= since + timedelta(seconds=max_age_s)
    ):
        state, reason = "manual", "RECONCILIATION_EXHAUSTED"
    if state in {"watching", "resolved"}:
        attempts, since = 0, None
    due_at = now + timedelta(seconds=outcome.delay_s)
    if state == "pending":
        due_at = min(due_at, since + timedelta(seconds=max_age_s))
    return record.model_copy(
        update={
            "state": state,
            "reason": reason,
            "attempts": attempts,
            "uncertain_since": since,
            "due_at": due_at,
            "token": None,
            "lease_until": None,
        }
    )
