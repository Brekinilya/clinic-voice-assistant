"""Общий замок журналов создания и отмены для автономного процесса."""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.booking.models import BookingAction
    from app.cancellation.models import CancelAction


class MemoryLedger:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.bookings: dict[str, BookingAction] = {}
        self.cancellations: dict[str, CancelAction] = {}
