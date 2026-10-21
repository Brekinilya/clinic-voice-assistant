"""Голосовой стенд целиком: приложение app.main, шлюз и WebSocket — с подставными моделями."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.settings import Settings
from app.voice import api
from app.voice.api import router
from app.voice.server import create_voice_app
from app.voice.settings import VoiceSettings
from tests.voice.fakes import QUIET, SPEECH, Dialogue, Recognizer, Synthesizer, Vad, reply


class Database:
    async def ping(self):
        pass

    async def aclose(self):
        pass


class VadModel:
    def stream(self):
        return Vad()


def voice_app(dialogue):
    @asynccontextmanager
    async def dialogue_factory(settings, clock):
        yield dialogue

    engines = SimpleNamespace(
        vad=VadModel(),
        recognizer=Recognizer({10: "Мне бы записаться к терапевту"}),
        synthesizer=Synthesizer(),
    )
    return create_voice_app(
        Settings(_env_file=None, backend_demo_enabled=True),
        VoiceSettings(_env_file=None, end_silence_ms=320, partial_ms=5000),
        engines,
        database_factory=lambda _: Database(),
        dialogue_factory=dialogue_factory,
    )


def next_event(socket, kind):
    while True:
        message = socket.receive()
        if message.get("text"):
            event = json.loads(message["text"])
            if event["type"] == kind:
                return event


def test_call_over_websocket():
    dialogue = Dialogue(lambda turn: reply("Назовите, пожалуйста, фамилию, имя, отчество."))
    with TestClient(voice_app(dialogue)) as client:
        assert client.get("/web/voice.html").status_code == 200
        assert "VoiceClient" in client.get("/web/voice-client.js").text
        with client.websocket_connect("/api/voice/call-1") as socket:
            assert next_event(socket, "ready") == {"type": "ready", "call_id": "call-1"}
            greeting = next_event(socket, "speak")
            assert greeting["text"].startswith("Здравствуйте!")
            assert socket.receive()["bytes"]

            socket.send_bytes(SPEECH * 10 + QUIET * 12)
            assert next_event(socket, "transcript")["text"] == "Мне бы записаться к терапевту"
            assert next_event(socket, "reply")["utterances"] == [
                "Назовите, пожалуйста, фамилию, имя, отчество."
            ]
            assert next_event(socket, "timing")["turn_id"] == 1
            socket.send_json({"type": "hangup"})
    assert [t.text for t in dialogue.turns] == ["Мне бы записаться к терапевту"]
    assert dialogue.ended


def test_voice_needs_engines_and_dialogue():
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as client:
        with client.websocket_connect("/api/voice/call-1") as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
    assert closed.value.code == 1013


async def test_server_hangup_after_client_left_does_not_escape(monkeypatch):
    # Шлюз закончил звонок, а клиент уже отключился: close() — WebSocketDisconnect(1006)
    class Gone:
        app = SimpleNamespace(state=SimpleNamespace(voice=object(), dialogue=object()))
        closed = 0

        async def accept(self):
            pass

        async def receive(self):
            await asyncio.Event().wait()

        async def close(self, code=1000, reason=None):
            self.closed += 1
            raise WebSocketDisconnect(code=1006)

    class Finished:
        def __init__(self):
            self.finished = asyncio.Event()
            self.closed = False

        async def start(self):
            self.finished.set()

        async def close(self):
            self.closed = True

    session = Finished()
    monkeypatch.setattr(api, "open_session", lambda state, call_id, channel: session)
    socket = Gone()
    await api.voice_call(socket, "call-1")
    assert session.closed and socket.closed == 1
