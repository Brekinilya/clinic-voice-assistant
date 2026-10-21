"""Атомарное сохранение заявок листа ожидания."""

from typing import Protocol

from app.waitlist.models import WaitlistRequest


class WaitlistStore(Protocol):
    async def create(self, request: WaitlistRequest) -> WaitlistRequest: ...
    async def by_call(self, call_id: str) -> tuple[WaitlistRequest, ...]: ...
    async def recent(self, limit: int = 100) -> tuple[WaitlistRequest, ...]: ...
