"""Потокобезопасное хранилище для автономного стенда."""

import asyncio

from app.waitlist.models import WaitlistRequest


class MemoryWaitlistStore:
    def __init__(self) -> None:
        self.requests: dict[str, WaitlistRequest] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: WaitlistRequest) -> WaitlistRequest:
        async with self._lock:
            existing = self.requests.get(request.request_id)
            if existing is not None:
                candidate = request.model_copy(update={"created_at": existing.created_at})
                if existing != candidate:
                    raise ValueError("WAITLIST_REQUEST_CONFLICT")
                return existing
            self.requests[request.request_id] = request
            return request

    async def by_call(self, call_id: str) -> tuple[WaitlistRequest, ...]:
        async with self._lock:
            return tuple(item for item in self.requests.values() if item.call_id == call_id)

    async def recent(self, limit: int = 100) -> tuple[WaitlistRequest, ...]:
        async with self._lock:
            return tuple(
                sorted(self.requests.values(), key=lambda item: item.created_at, reverse=True)[
                    :limit
                ]
            )
