"""Ожидание свободного окна; храним только ссылку на пациента и пожелания."""

from pydantic import AwareDatetime, Field

from app.booking.models import BookingModel
from app.booking.preferences import SlotPreferences
from app.mis.models import MisId


class WaitlistRequest(BookingModel):
    request_id: str = Field(min_length=32, max_length=64)
    call_id: str = Field(min_length=1, max_length=128)
    turn_id: int = Field(ge=1)
    client_id: MisId = Field(repr=False)
    specialty: str = Field(min_length=1, max_length=100)
    preferences: SlotPreferences
    created_at: AwareDatetime


class WaitlistError(Exception):
    """Машиночитаемая ошибка создания заявки листа ожидания."""
