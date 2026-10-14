"""WebSocket голосового шлюза: /api/voice/{call_id}.

Клиент → шлюз: бинарные кадры PCM 16 бит, 16 кГц, моно; {"type": "hangup"} — положить трубку.
Шлюз → клиент: события JSON — ready, transcript, reply, speak, stop, timing, end. Звук ответа идёт
бинарным кадром PCM 16 бит сразу после {"type": "speak", "sample_rate": …}; stop — сбросить очередь.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.clock import ClinicClock
from app.dialogue.models import CallId
from app.voice.session import CallSession

log = logging.getLogger(__name__)
router = APIRouter()


class SocketChannel:
    def __init__(self, socket: WebSocket):
        self.socket = socket
        # Ответы, перебивание и замеры отправляют разные задачи звонка
        self._lock = asyncio.Lock()

    async def send_json(self, message: dict) -> None:
        async with self._lock:
            await self.socket.send_text(json.dumps(message, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            await self.socket.send_bytes(data)


@router.websocket("/api/voice/{call_id}")
async def voice_call(socket: WebSocket, call_id: CallId) -> None:
    state = socket.app.state
    engines = getattr(state, "voice", None)
    dialogue = getattr(state, "dialogue", None)
    await socket.accept()
    if engines is None or dialogue is None:
        await socket.close(code=1013, reason="VOICE_DISABLED")
        return
    runtime = getattr(state, "runtime", None)
    clock = getattr(runtime, "clock", None) or ClinicClock()
    session = CallSession(
        call_id,
        SocketChannel(socket),
        dialogue,
        engines.vad.stream(),
        engines.recognizer,
        engines.synthesizer,
        today=lambda: clock.now().date(),
        settings=getattr(state, "voice_settings", None),
    )

    async def receive() -> None:
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                await session.feed(message["bytes"])
            elif message.get("text"):
                if json.loads(message["text"]).get("type") == "hangup":
                    return

    try:
        await session.start()
        listener = asyncio.create_task(receive())
        finished = asyncio.create_task(session.finished.wait())
        done, pending = await asyncio.wait(
            {listener, finished}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("голосовой звонок прерван")
    finally:
        await session.close()
        try:
            await socket.close()
        except RuntimeError:
            pass
