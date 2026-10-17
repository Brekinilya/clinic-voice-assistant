"""Общий скользящий бюджет: отказ сразу, ожидание оставляем вызывающему сценарию."""

import asyncio
import math
from collections import deque
from datetime import timedelta

from app.mis.ports import BudgetExhausted
from app.ports import Clock


class MemoryBudget:
    """Только для автономного стенда: не разделяется между процессами."""

    def __init__(self, clock: Clock, limit: int = 8) -> None:
        if not 1 <= limit <= 8:
            raise ValueError("бюджет должен быть от 1 до 8")
        self._clock = clock
        self._limit = limit
        self._requests = deque()
        self._blocked_until = clock.now()
        self._lock = asyncio.Lock()

    async def reserve(self) -> None:
        async with self._lock:
            now = self._clock.now()
            while self._requests and self._requests[0] <= now - timedelta(seconds=60):
                self._requests.popleft()
            delay = (self._blocked_until - now).total_seconds()
            if len(self._requests) >= self._limit:
                delay = max(delay, 60 - (now - self._requests[0]).total_seconds())
            if delay > 0:
                raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=max(1, math.ceil(delay)))
            self._requests.append(now)

    async def defer(self, seconds: int) -> None:
        async with self._lock:
            self._blocked_until = max(
                self._blocked_until, self._clock.now() + timedelta(seconds=seconds)
            )
