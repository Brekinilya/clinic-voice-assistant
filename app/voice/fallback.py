"""Короткие заранее записанные реплики: для них не нужен работающий TTS."""

import hashlib
import json
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class FallbackAudio:
    text: str
    pcm: bytes
    rate: int


@lru_cache(maxsize=2)
def load_fallback(emergency: bool = False) -> FallbackAudio:
    directory = Path(__file__).with_name("assets")
    name = "emergency" if emergency else "unavailable"
    manifest = json.loads((directory / "fallbacks.json").read_text(encoding="utf-8"))
    record = manifest[name]
    path = directory / record["file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise RuntimeError("повреждена резервная голосовая реплика")
    with wave.open(str(path)) as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise RuntimeError("неподдерживаемый формат резервной реплики")
        return FallbackAudio(
            record["text"], source.readframes(source.getnframes()), source.getframerate()
        )
