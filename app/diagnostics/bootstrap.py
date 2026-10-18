"""Ресурсы диагностики живут столько же, сколько приложение."""

import asyncio
from contextlib import asynccontextmanager, suppress
from secrets import token_bytes

from app.diagnostics.recorder import EventStore, Recorder


@asynccontextmanager
async def open_diagnostics(settings, service):
    if not settings.debug_panel_enabled:
        yield None
        return
    engine = getattr(service.booking.store, "engine", None)
    key = settings.dialogue_hash_key.get_secret_value().encode() or token_bytes(32)
    recorder = Recorder(EventStore(key, engine))
    service.debug = recorder
    worker = asyncio.create_task(recorder.run())
    try:
        yield recorder
    finally:
        recorder.closed = True
        with suppress(TimeoutError):
            await recorder.flush()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        service.debug = None
