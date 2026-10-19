"""Журнал заявок: состояние, маркер и отпечаток описания — сам текст карточки не хранится."""

import hashlib
import secrets
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.mis.models import ESCALATION_LIMIT, MisId

EscalationState = Literal["prepared", "sending", "applied", "unknown", "manual"]
Priority = Literal["P1", "P2", "P3"]
MisStatus = Literal["applied", "pending", "manual"]
# После неизвестного исхода POST только читаем список: столько раз, затем — человек
CHECKS = 3
CHECK_AFTER = timedelta(seconds=30)


def new_marker() -> str:
    return "№" + secrets.token_hex(5)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def compose(card: str, marker: str) -> str:
    """Карточка и строка с маркером в пределах 1000 символов; режем свою карточку, не маркер."""
    tail = f"\nЗаявка ассистента {marker}"
    # Стенд отклоняет управляющие символы кроме перевода строки и табуляции: карточка наша,
    # поэтому заменяем их пробелом, а не теряем заявку на 400
    cleaned = "".join(
        " " if (ord(c) < 32 and c not in "\t\n") or 127 <= ord(c) <= 159 else c for c in card
    )
    body = "".join(c for c in cleaned if not 0xD800 <= ord(c) <= 0xDFFF).strip()
    body = body or "Причина не указана"
    room = ESCALATION_LIMIT - len(tail)
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return body + tail


class EscalationRecord(BookingModel):
    # Один звонок и один маршрут — одна заявка: повтор возвращает ту же запись
    id: str = Field(min_length=1, max_length=200)
    call_id: str = Field(min_length=1, max_length=128)
    route: str = Field(pattern=r"^R-\d{2}$")
    priority: Priority
    client_id: MisId | None = Field(default=None, repr=False)
    # Личность не установлена: заявка на заранее заведённого технического клиента
    technical: bool = False
    marker: str
    length: int = Field(ge=1, le=ESCALATION_LIMIT)
    digest: str = Field(min_length=64, max_length=64)
    state: EscalationState = "prepared"
    escalation_id: MisId | None = None
    checks: int = Field(default=0, ge=0)
    reason: str | None = None
    next_check_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EscalationStatus(BookingModel):
    """Что диалог знает о заявке: applied — стенд её принял, pending — сверяем, manual — человек."""

    ref: str
    route: str
    priority: Priority
    mis_status: MisStatus
    escalation_id: MisId | None = None
    technical: bool = False
    reason: str | None = None

    @classmethod
    def of(cls, record: EscalationRecord) -> "EscalationStatus":
        status = {"applied": "applied", "manual": "manual"}.get(record.state, "pending")
        return cls(
            ref=record.id,
            route=record.route,
            priority=record.priority,
            mis_status=status,
            escalation_id=record.escalation_id,
            technical=record.technical,
            reason=record.reason,
        )
