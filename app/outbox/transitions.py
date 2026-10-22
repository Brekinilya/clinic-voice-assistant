"""Общие переходы двух хранилищ и защита от просроченного обработчика."""

from datetime import datetime, timedelta
from uuid import uuid4

from app.outbox.models import (
    MAX_ATTEMPTS,
    DeliveryReceipt,
    JobState,
    OutboxError,
    OutboxJob,
    prepare_comment_event,
)


def changed(job: OutboxJob, **updates) -> OutboxJob:
    # model_copy не проверяет инварианты; переход всегда проверяем полностью.
    return OutboxJob.model_validate({**job.model_dump(), **updates})


def expire(job: OutboxJob, now: datetime) -> OutboxJob:
    if job.state == JobState.PROCESSING and job.lease_until <= now:
        return changed(
            job,
            state=JobState.UNKNOWN,
            lease_token=None,
            lease_until=None,
            last_error="WORKER_LOST",
        )
    return job


def claim(job: OutboxJob, now: datetime, lease_seconds: int) -> OutboxJob:
    if not 1 <= lease_seconds <= 300:
        raise ValueError("аренда должна быть от 1 до 300 секунд")
    if job.state not in {JobState.PENDING, JobState.RETRY} or job.available_at > now:
        raise OutboxError("JOB_NOT_READY")
    if job.attempts >= MAX_ATTEMPTS:
        raise OutboxError("ATTEMPTS_EXHAUSTED")
    prepared = prepare_comment_event(job, now)
    return changed(
        prepared,
        state=JobState.PROCESSING,
        attempts=job.attempts + 1,
        lease_token=uuid4().hex,
        lease_until=now + timedelta(seconds=lease_seconds),
        last_error=None,
    )


def finish(
    job: OutboxJob,
    token: str,
    state: JobState,
    now: datetime,
    *,
    receipt: DeliveryReceipt | None = None,
    retry_after: int = 0,
    error: str | None = None,
) -> OutboxJob:
    if job.state != JobState.PROCESSING or job.lease_token != token or job.lease_until <= now:
        raise OutboxError("LEASE_LOST")
    if state not in {
        JobState.RETRY,
        JobState.UNKNOWN,
        JobState.BLOCKED,
        JobState.ACKNOWLEDGED,
        JobState.FAILED,
    }:
        raise OutboxError("INVALID_TRANSITION")
    if state == JobState.RETRY and job.attempts >= MAX_ATTEMPTS:
        state = JobState.FAILED
    return changed(
        job,
        state=state,
        lease_token=None,
        lease_until=None,
        available_at=now + timedelta(seconds=max(1, min(3600, retry_after))),
        receipt=receipt,
        last_error=error,
    )


def acknowledge_unknown(job: OutboxJob, receipt: DeliveryReceipt) -> OutboxJob:
    if job.state == JobState.ACKNOWLEDGED and job.receipt == receipt:
        return job
    if job.state != JobState.UNKNOWN:
        raise OutboxError("RECONCILIATION_NOT_REQUIRED")
    return changed(job, state=JobState.ACKNOWLEDGED, receipt=receipt, last_error=None)


def resume_blocked(job: OutboxJob, now: datetime) -> OutboxJob:
    if job.state != JobState.BLOCKED:
        raise OutboxError("JOB_NOT_BLOCKED")
    # В blocked нет принятого действия, поэтому повтор после настройки адаптера допустим.
    return changed(job, state=JobState.PENDING, available_at=now, attempts=0, last_error=None)
