"""Начало и конец фразы по вероятности речи в каждом кадре 32 мс."""

from collections import deque
from dataclasses import dataclass

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # столько ждёт Silero VAD на 16 кГц
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE


@dataclass
class EndpointSettings:
    speech_threshold: float = 0.5
    # Ниже этого — тишина; между порогами кадр не меняет состояние
    silence_threshold: float = 0.35
    min_speech_ms: int = 192
    end_silence_ms: int = 640
    max_phrase_ms: int = 15000
    # Сколько звука до начала речи отдать распознаванию, чтобы не потерять первый слог
    preroll_ms: int = 320
    # Сколько тишины после речи оставить во фразе
    tail_ms: int = 192


class Endpointer:
    """push(кадр, вероятность) → "start", "end" или None. После "end" фраза лежит в take()."""

    def __init__(self, settings: EndpointSettings | None = None):
        self.settings = settings or EndpointSettings()
        self.end_silence_ms = self.settings.end_silence_ms
        self._preroll: deque[bytes] = deque(maxlen=self.settings.preroll_ms // FRAME_MS)
        self._pending: list[bytes] = []
        self._voiced = 0
        self._phrase: list[bytes] = []
        self._silent = 0
        self._finished = b""
        self.in_phrase = False

    def push(self, frame: bytes, probability: float) -> str | None:
        s = self.settings
        if self.in_phrase:
            self._phrase.append(frame)
            if probability < s.silence_threshold:
                self._silent += 1
            elif probability >= s.speech_threshold:
                self._silent = 0
            too_long = len(self._phrase) * FRAME_MS >= s.max_phrase_ms
            if self._silent * FRAME_MS >= self.end_silence_ms or too_long:
                return self._finish()
            return None

        if self._pending:
            self._pending.append(frame)
            if probability >= s.speech_threshold:
                self._voiced += 1
            elif probability < s.silence_threshold:
                # Короткий всплеск — кашель или щелчок, а не речь
                self._preroll.extend(self._pending)
                self._pending, self._voiced = [], 0
                return None
            if self._voiced * FRAME_MS >= s.min_speech_ms:
                self._phrase = [*self._preroll, *self._pending]
                self._preroll.clear()
                self._pending, self._voiced, self._silent = [], 0, 0
                self.in_phrase = True
                return "start"
            return None

        if probability >= s.speech_threshold:
            self._pending, self._voiced = [frame], 1
            return None
        self._preroll.append(frame)
        return None

    def audio(self) -> bytes:
        """Звук текущей фразы — для промежуточного распознавания."""
        return b"".join(self._phrase)

    @property
    def phrase_ms(self) -> int:
        return len(self._phrase) * FRAME_MS if self.in_phrase else 0

    def take(self) -> bytes:
        audio, self._finished = self._finished, b""
        return audio

    def _finish(self) -> str:
        keep = len(self._phrase) - max(0, self._silent - self.settings.tail_ms // FRAME_MS)
        self._finished = b"".join(self._phrase[:keep])
        self._phrase, self._silent = [], 0
        self.in_phrase = False
        return "end"
