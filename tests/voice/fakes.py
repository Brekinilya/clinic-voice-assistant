"""Подставные VAD, распознавание, синтез, диалог и канал для тестов голосового шлюза."""

import asyncio
import time
from types import SimpleNamespace

from app.voice.endpoint import FRAME_BYTES

SPEECH = b"\x10\x00" * (FRAME_BYTES // 2)
QUIET = bytes(FRAME_BYTES)


def phrase(words: int) -> bytes:
    """«Речь» длиной words кадров и тишина, после которой фраза считается законченной."""
    return SPEECH * words + QUIET * 12


def reply(*utterances, **fields):
    defaults = {"kind": "reply", "phase": "ready", "transfer": None, "code": None}
    return SimpleNamespace(utterances=utterances, **{**defaults, **fields})


class Vad:
    def probability(self, frame: bytes) -> float:
        return 0.9 if frame == SPEECH else 0.05


class Recognizer:
    """Текст по числу кадров речи во фразе; delay — распознавание небыстрое."""

    def __init__(self, texts: dict[int, str], delay: float = 0.0):
        self.texts = texts
        self.delay = delay
        self.heard: list[int] = []

    def transcribe(self, pcm: bytes) -> str:
        if self.delay:
            time.sleep(self.delay)
        words = sum(pcm[i : i + FRAME_BYTES] == SPEECH for i in range(0, len(pcm), FRAME_BYTES))
        self.heard.append(words)
        return self.texts.get(words, "")


class Synthesizer:
    def __init__(self, seconds: dict[str, float] | None = None):
        self.seconds = seconds or {}
        self.spoken: list[str] = []

    def synthesize(self, speech: str) -> tuple[bytes, int]:
        self.spoken.append(speech)
        length = self.seconds.get(speech.replace("*", ""), 0.02)
        return b"\x00\x00" * int(1000 * length), 1000


class Dialogue:
    """Как диалог Александра: на промежуточную расшифровку — kind="partial", если не urgent."""

    def __init__(self, answer, delay: float = 0.0, urgent: bool = False):
        self.answer = answer
        self.delay = delay
        self.urgent = urgent
        self.calls = []
        self.ended = False

    @property
    def turns(self):
        return [t for t in self.calls if t.is_final]

    async def handle(self, call_id, turn):
        self.calls.append(turn)
        if not turn.is_final and not self.urgent:
            return reply("Ожидаю завершения команды.", kind="partial")
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer(turn)

    async def end(self, call_id):
        self.ended = True


class Channel:
    def __init__(self):
        self.events: list[dict] = []
        self.audio: list[bytes] = []

    async def send_json(self, message):
        self.events.append(message)

    async def send_bytes(self, data):
        self.audio.append(data)

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]

    def said(self) -> list[str]:
        return [e["text"] for e in self.of("speak")]
