"""Общий скользящий бюджет МИС: приоритет, ограниченное ожидание, отмена и предоплата.

Окно — не больше limit запросов за 60 с на токен команды. Срочные операции пользуются
всем окном, остальные оставляют им запас: свежая проверка и запись после согласия
не ждут за справкой или фоновой задачей. Ждать разрешено только до срока из Demand;
дальше вызывающий сценарий получает BudgetExhausted с честным retry_after.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import IntEnum

from app.mis.ports import BudgetCancelled, BudgetExhausted
from app.ports import Clock

WINDOW_S = 60
# Пока окно занято, ожидающий проверяет отмену не реже этого интервала.
POLL_S = 0.5
# Сколько талон ждёт звонящего после обещанного времени: ответ «подождите» и новая фраза.
HOLD = timedelta(seconds=20)


class Priority(IntEnum):
    COMMIT = 0  # свежая проверка и запись после согласия
    RECONCILE = 1  # сверка неизвестного исхода
    DIALOGUE = 2  # чтение для живого разговора
    BACKGROUND = 3  # каталог, служебные и будущие исходящие задания


# Какую долю окна приоритет оставляет более срочным запросам. При лимите 8 разговор
# оставляет записи 1 место, фоновые задания — 3; маленький лимит запаса не даёт.
HEADROOM = {
    Priority.COMMIT: 0,
    Priority.RECONCILE: 0,
    Priority.DIALOGUE: 1 / 8,
    Priority.BACKGROUND: 3 / 8,
}


def capacity(limit: int, priority: Priority) -> int:
    return max(1, limit - math.floor(limit * HEADROOM[priority]))


@dataclass(frozen=True)
class Demand:
    """Кто просит запрос: приоритет, крайний срок ожидания и признак отмены."""

    priority: Priority = Priority.DIALOGUE
    # Без срока бюджет не ждёт: отказ сразу, как и раньше.
    until: datetime | None = None
    cancelled: Callable[[], Awaitable[bool]] | None = None
    # Звонок или задание: продолжать прерванное постраничное чтение можно только своё.
    owner: str | None = None
    # Сообщает о выданных местах: по ним сценарий видит, что ожидание не стоит на месте.
    granted: Callable[[int], None] | None = None


_demand: ContextVar[Demand] = ContextVar("mis_demand", default=Demand())


def current_demand() -> Demand:
    return _demand.get()


@contextmanager
def demand(
    priority: Priority | None = None,
    *,
    until: datetime | None = None,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
    owner: str | None = None,
    granted: Callable[[int], None] | None = None,
) -> Iterator[Demand]:
    """Вложенный вызов меняет приоритет, но не продлевает срок и не снимает отмену."""
    outer = _demand.get()
    deadline = outer.until if until is None else until
    if outer.until is not None and until is not None:
        deadline = min(outer.until, until)
    checks = [check for check in (outer.cancelled, cancelled) if check is not None]

    async def any_cancelled() -> bool:
        for check in checks:
            if await check():
                return True
        return False

    value = replace(
        outer,
        priority=outer.priority if priority is None else priority,
        until=deadline,
        cancelled=None if not checks else checks[0] if len(checks) == 1 else any_cancelled,
        owner=outer.owner if owner is None else owner,
        granted=outer.granted if granted is None else granted,
    )
    token = _demand.set(value)
    try:
        yield value
    finally:
        _demand.reset(token)


@contextmanager
def immediate() -> Iterator[Demand]:
    """Без ожидания окна: запись после журнала не ждёт, иначе согласие успело бы устареть."""
    token = _demand.set(replace(_demand.get(), until=None))
    try:
        yield _demand.get()
    finally:
        _demand.reset(token)


def delay(
    now: datetime,
    stamps: list[datetime],
    blocked_until: datetime | None,
    *,
    limit: int,
    priority: Priority,
    cost: int,
    window_s: float = WINDOW_S,
) -> float:
    """Через сколько секунд cost запросов этого приоритета уложатся в окно; 0 — сейчас."""
    room = capacity(limit, priority)
    if not 0 <= cost <= room:
        raise ValueError("стоимость операции больше доступного окна")
    window = timedelta(seconds=window_s)
    active = sorted(t for t in stamps if t > now - window)
    wait = 0.0 if blocked_until is None else max(0.0, (blocked_until - now).total_seconds())
    excess = len(active) + cost - room
    if cost and excess > 0:
        wait = max(wait, (active[excess - 1] + window - now).total_seconds())
    return wait


@dataclass(frozen=True)
class Row:
    """Место окна: отправленный запрос или талон, закреплённый за ждущим звонком."""

    id: object
    at: datetime
    owner: str | None = None
    hold_until: datetime | None = None


@dataclass(frozen=True)
class Decision:
    wait: float
    # Свои наступившие талоны, которые становятся запросами сейчас.
    take: tuple = ()
    # Сколько новых мест занять сейчас.
    need: int = 0
    # Когда закрепить cost мест за звонком, если ждать дольше срока.
    book_at: datetime | None = None


def decide(
    now: datetime,
    rows: list[Row],
    blocked_until: datetime | None,
    *,
    limit: int,
    priority: Priority,
    cost: int,
    owner: str | None,
    book: bool,
    window_s: float = WINDOW_S,
) -> Decision:
    """Решение бюджета без хранилища: общее для памяти и PostgreSQL.

    Талон — обещание звонку «подождите N секунд». С наступлением его времени талон
    держит место окна, пока звонок не вернётся или срок талона не истечёт: к обещанному
    моменту место свободно, а не досталось заговорившему первым. Раньше этого времени
    талон окно не занимает. Наступившие талоны обслуживаются по порядку выдачи.
    """
    if not 1 <= cost <= capacity(limit, priority):
        raise ValueError("стоимость операции больше доступного окна")
    mine = [r for r in rows if owner is not None and r.owner == owner]
    due = sorted((r for r in mine if r.at <= now), key=lambda r: r.id)
    if mine and not due:
        # Места уже обещаны: ждём своего времени, вторую очередь не занимаем.
        return Decision(wait=(min(r.at for r in mine) - now).total_seconds())
    first = min((r.id for r in mine), default=None)
    take = tuple(r.id for r in due[:cost])
    stamps = [r.at for r in rows if r.owner is None] + [
        now
        for r in rows
        if r.owner is not None
        and r.owner != owner
        and r.at <= now
        and (first is None or r.id < first)
    ]
    wait = delay(
        now,
        stamps,
        blocked_until,
        limit=limit,
        priority=priority,
        cost=cost,
        window_s=window_s,
    )
    if wait > 0:
        if not (book and owner is not None and not mine):
            return Decision(wait=wait)
        # Обещание должно быть выполнимым: учитываем все уже выданные талоны,
        # в том числе ещё не наступившие, — каждый займёт окно со своего времени.
        promised = delay(
            now,
            [r.at for r in rows if r.owner is None] + [max(r.at, now) for r in rows if r.owner],
            blocked_until,
            limit=limit,
            priority=priority,
            cost=cost,
            window_s=window_s,
        )
        return Decision(wait=promised, book_at=now + timedelta(seconds=promised))
    return Decision(wait=0.0, take=take, need=cost - len(take))


async def admit(
    attempt: Callable[[Priority, int, str | None, bool], Awaitable[tuple[list | None, float]]],
    cost: int,
    clock: Clock,
    sleep: Callable[[float], Awaitable[object]],
) -> list:
    """Пробуем занять окно до срока. Ожидание прерывается отменой и не начинается напрасно.

    Если до срока окна не будет, звонок получает талон на обещанное время.
    Фоновым задачам талоны не выдаются: они уступают разговорам.
    """
    current = _demand.get()
    # Талон нужен тому, кто ждёт до срока и скажет звонящему «подождите»; без срока не ждут.
    book = (
        current.owner is not None
        and current.until is not None
        and current.priority < Priority.BACKGROUND
    )
    while True:
        places, wait = await attempt(current.priority, cost, current.owner, False)
        if places is None and current.cancelled is not None and await current.cancelled():
            raise BudgetCancelled("WAIT_CANCELLED")
        left = 0.0 if current.until is None else (current.until - clock.now()).total_seconds()
        if places is None and wait > left and book:
            places, wait = await attempt(current.priority, cost, current.owner, True)
        if places is not None:
            if current.granted is not None:
                current.granted(cost)
            return places
        if wait > left:
            raise BudgetExhausted("LOCAL_RATE_LIMIT", retry_after=max(1, math.ceil(wait)))
        await sleep(min(wait, POLL_S))


class MemoryBudget:
    """Только для автономного стенда: не разделяется между процессами."""

    def __init__(
        self,
        clock: Clock,
        limit: int = 8,
        *,
        window_s: float = WINDOW_S,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if not 1 <= limit <= 8:
            raise ValueError("бюджет должен быть от 1 до 8")
        self._clock = clock
        self._limit = limit
        self._window_s = window_s
        self._sleep = sleep
        self._requests: dict[int, Row] = {}
        self._next_id = 0
        self._blocked_until = clock.now()
        self._lock = asyncio.Lock()

    def _add(self, at: datetime, owner=None, hold_until=None) -> int:
        self._next_id += 1
        self._requests[self._next_id] = Row(self._next_id, at, owner, hold_until)
        return self._next_id

    async def _attempt(
        self, priority: Priority, cost: int, owner: str | None, book: bool
    ) -> tuple[list | None, float]:
        async with self._lock:
            now = self._clock.now()
            horizon = now - timedelta(seconds=self._window_s)
            for key, row in list(self._requests.items()):
                if (row.owner is None and row.at <= horizon) or (
                    row.hold_until is not None and row.hold_until <= now
                ):
                    del self._requests[key]
            decision = decide(
                now,
                list(self._requests.values()),
                self._blocked_until,
                limit=self._limit,
                priority=priority,
                cost=cost,
                owner=owner,
                book=book,
                window_s=self._window_s,
            )
            if decision.wait > 0:
                if decision.book_at is not None:
                    for _ in range(cost):
                        self._add(decision.book_at, owner, decision.book_at + HOLD)
                return None, decision.wait
            for key in decision.take:
                self._requests[key] = Row(key, now)
            return [*decision.take, *(self._add(now) for _ in range(decision.need))], 0.0

    async def reserve(self, cost: int = 1) -> list:
        return await admit(self._attempt, cost, self._clock, self._sleep)

    async def release(self, ids: list) -> None:
        # Неиспользованная предоплата: HTTP не отправлялся, место возвращается окну.
        async with self._lock:
            for key in ids:
                self._requests.pop(key, None)

    async def touch(self, place: int) -> float:
        """Отметить отправку предоплаченного места; вернуть остаток паузы после 429."""
        async with self._lock:
            now = self._clock.now()
            paused = (self._blocked_until - now).total_seconds()
            if paused > 0:
                return paused
            if place in self._requests:
                self._requests[place] = Row(place, now)
            return 0.0

    async def defer(self, seconds: int) -> None:
        async with self._lock:
            self._blocked_until = max(
                self._blocked_until, self._clock.now() + timedelta(seconds=seconds)
            )
