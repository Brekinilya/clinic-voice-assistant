"""Ограниченный журнал диагностики. Тексты шифруются, запись вынесена из аудиоцикла."""

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import monotonic
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import case, delete, func, insert, select, text

from app.storage.schema import debug_events

RETENTION = timedelta(hours=1)
MAX_EVENTS = 10000
MAX_PAYLOAD = 16000
_current = ContextVar("diagnostic_call", default=None)


class EventStore:
    """Один интерфейс для изолированного стенда и общего PostgreSQL на ВМ."""

    def __init__(self, key: bytes, engine=None):
        derived = hmac.new(key, b"clinic-debug-v1", hashlib.sha256).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(derived))
        self.engine = engine
        self.rows = deque(maxlen=MAX_EVENTS)
        self.sequence = 0

    async def append(self, events):
        now = datetime.now(timezone.utc)
        rows = [
            {**event, "payload": self.cipher.encrypt(event["payload"].encode()).decode()}
            for event in events
        ]
        if self.engine is None:
            self.rows = deque(
                (r for r in self.rows if r["created_at"] > now - RETENTION), maxlen=MAX_EVENTS
            )
            for row in rows:
                self.sequence += 1
                self.rows.append({"id": self.sequence, **row})
            return
        async with self.engine.begin() as connection:
            # Последовательность ID должна совпадать с порядком фиксации между процессами.
            await connection.execute(text("SELECT pg_advisory_xact_lock(142008)"))
            await connection.execute(insert(debug_events), rows)
            await connection.execute(
                delete(debug_events).where(debug_events.c.created_at <= now - RETENTION)
            )
            cutoff = await connection.scalar(
                select(debug_events.c.id)
                .order_by(debug_events.c.id.desc())
                .offset(MAX_EVENTS)
                .limit(1)
            )
            if cutoff is not None:
                await connection.execute(delete(debug_events).where(debug_events.c.id <= cutoff))

    async def read(self, call_id=None, after=0, limit=500):
        now = datetime.now(timezone.utc)
        if self.engine is None:
            rows = [
                row
                for row in self.rows
                if row["created_at"] > now - RETENTION
                and row["id"] > after
                and (call_id is None or row["call_id"] == call_id)
            ]
            rows = rows[:limit] if call_id else rows[-limit:]
        else:
            query = select(debug_events).where(
                debug_events.c.created_at > now - RETENTION, debug_events.c.id > after
            )
            if call_id:
                query = query.where(debug_events.c.call_id == call_id).order_by(debug_events.c.id)
            else:
                # Список последних звонков не зависит от количества реплик в одном звонке.
                calls = (
                    select(debug_events.c.call_id)
                    .where(debug_events.c.created_at > now - RETENTION)
                    .group_by(debug_events.c.call_id)
                    .order_by(func.max(debug_events.c.id).desc())
                    .limit(30)
                )
                query = query.where(debug_events.c.call_id.in_(calls)).order_by(
                    debug_events.c.id.desc()
                )
            async with self.engine.connect() as connection:
                rows = list((await connection.execute(query.limit(limit))).mappings())
            if call_id is None:
                rows.reverse()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(self.cipher.decrypt(item.pop("payload").encode()))
            except (InvalidToken, ValueError):
                item.pop("payload", None)
                item["data"] = {"unavailable": "Ключ журнала изменился или запись повреждена"}
            result.append(item)
        return result

    async def calls(self):
        cutoff = datetime.now(timezone.utc) - RETENTION
        ended = {"dialogue.ended", "voice.end", "voice.closed"}
        if self.engine is None:
            items = {}
            for event in self.rows:
                if event["created_at"] <= cutoff:
                    continue
                item = items.setdefault(
                    event["call_id"],
                    {"call_id": event["call_id"], "source": "text", "ended": False},
                )
                item.update(last_at=event["created_at"], last_id=event["id"])
                if event["source"] != "text":
                    item["source"] = event["source"]
                item["ended"] |= event["kind"] in ended
            return sorted(items.values(), key=lambda item: item["last_id"], reverse=True)[:30]
        # Для списка звонков тексты не читаются и не расшифровываются.
        query = (
            select(
                debug_events.c.call_id,
                func.max(debug_events.c.id).label("last_id"),
                func.max(debug_events.c.created_at).label("last_at"),
                func.bool_or(debug_events.c.kind.in_(ended)).label("ended"),
                func.coalesce(
                    func.max(case((debug_events.c.source != "text", debug_events.c.source))), "text"
                ).label("source"),
            )
            .where(debug_events.c.created_at > cutoff)
            .group_by(debug_events.c.call_id)
            .order_by(func.max(debug_events.c.id).desc())
            .limit(30)
        )
        async with self.engine.connect() as connection:
            return [dict(row) for row in (await connection.execute(query)).mappings()]

    async def purge(self, call_id):
        if self.engine is None:
            self.rows = deque((r for r in self.rows if r["call_id"] != call_id), maxlen=MAX_EVENTS)
        else:
            async with self.engine.begin() as connection:
                await connection.execute(
                    delete(debug_events).where(debug_events.c.call_id == call_id)
                )

    async def cleanup(self):
        cutoff = datetime.now(timezone.utc) - RETENTION
        if self.engine is None:
            self.rows = deque((r for r in self.rows if r["created_at"] > cutoff), maxlen=MAX_EVENTS)
        else:
            async with self.engine.begin() as connection:
                await connection.execute(
                    delete(debug_events).where(debug_events.c.created_at <= cutoff)
                )


class Recorder:
    def __init__(self, store, *, capacity=512):
        self.store = store
        self.queue = asyncio.Queue(maxsize=capacity)
        self.producer = uuid4().hex[:12]
        self.dropped = 0
        self.healthy = True
        self.closed = False
        self.blocked = set()
        self.pending_purges = set()

    def emit(self, call_id, turn_id, event_kind, **data):
        if self.closed:
            return
        try:
            payload = json.dumps(
                {"recording": "opt_out"}
                if call_id in self.blocked
                else {**data, "diagnostic_dropped": self.dropped},
                ensure_ascii=False,
                default=str,
            )
            if len(payload.encode()) > MAX_PAYLOAD:
                payload = json.dumps({"truncated": True, "reason": "Событие слишком большое"})
            self.queue.put_nowait(
                {
                    "call_id": call_id,
                    "turn_id": turn_id,
                    "kind": event_kind,
                    "producer": self.producer,
                    "source": data.get("source")
                    if data.get("source") in {"SIP", "browser"}
                    else "text",
                    "created_at": datetime.now(timezone.utc),
                    "payload": payload,
                }
            )
        except (asyncio.QueueFull, TypeError, ValueError):
            self.dropped += 1

    def opt_out(self, call_id):
        # Отказ не теряется при заполнении очереди. Уже накопленные тексты также удаляются.
        if call_id in self.blocked:
            return
        if len(self.blocked) >= MAX_EVENTS:
            self.blocked.pop()
        self.blocked.add(call_id)
        self.pending_purges.add(call_id)
        self.emit(call_id, None, "privacy.opt_out")

    async def run(self):
        while True:
            try:
                async with asyncio.timeout(15):
                    item = await self.queue.get()
            except TimeoutError:
                try:
                    async with asyncio.timeout(1):
                        await self.store.cleanup()
                except Exception:
                    self.healthy = False
                continue
            try:
                async with asyncio.timeout(1):
                    for call_id in tuple(self.pending_purges):
                        await self.store.purge(call_id)
                        await self.store.append(
                            [
                                {
                                    "call_id": call_id,
                                    "turn_id": None,
                                    "kind": "privacy.opt_out",
                                    "producer": self.producer,
                                    "source": "text",
                                    "created_at": datetime.now(timezone.utc),
                                    "payload": '{"recording":"opt_out"}',
                                }
                            ]
                        )
                        self.pending_purges.discard(call_id)
                    if item["call_id"] in self.blocked:
                        item = {**item, "payload": '{"recording":"opt_out"}'}
                    await self.store.append([item])
                self.healthy = True
            except Exception:
                # Ошибка диагностики не меняет результат записи и не останавливает аудио.
                self.dropped += 1
                self.healthy = False
            finally:
                self.queue.task_done()

    async def flush(self):
        async with asyncio.timeout(2):
            await self.queue.join()

    def status(self):
        return {
            "healthy": self.healthy,
            "dropped": self.dropped,
            "queued": self.queue.qsize(),
            "storage": "postgres" if self.store.engine is not None else "memory",
            "retention_minutes": 60,
            "producer": self.producer,
        }


@contextmanager
def observe(recorder, call_id, turn_id):
    token = _current.set((recorder, call_id, turn_id))
    try:
        yield
    finally:
        _current.reset(token)


def emit(event_kind, **data):
    current = _current.get()
    if current is not None and current[0] is not None:
        current[0].emit(current[1], current[2], event_kind, **data)


def opt_out():
    current = _current.get()
    if current is not None and current[0] is not None:
        current[0].opt_out(current[1])


def resource_path(path):
    # В диагностику МИС не попадают query, тела, токен и персональные идентификаторы.
    path = path.split("?", 1)[0]
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)


def trace_mis_request(request):
    """Наблюдение вокруг единственной точки отправки; её бюджет и повторы не меняются."""

    @wraps(request)
    async def traced(self, method, path, **kwargs):
        from app.mis.ports import MisError

        started = monotonic()
        emit("mis.request", method=method, resource=resource_path(path))
        try:
            return await request(self, method, path, **kwargs)
        except Exception as error:
            emit(
                "mis.error",
                error=type(error).__name__,
                code=error.code if isinstance(error, MisError) else None,
                retry_after=error.retry_after if isinstance(error, MisError) else None,
            )
            raise
        finally:
            emit("mis.finished", elapsed_ms=round((monotonic() - started) * 1000))

    return traced
