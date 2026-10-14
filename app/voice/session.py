"""Один звонок через голосовой шлюз: фраза пациента → распознавание → диалог → синтез.

Каждая фраза пациента — новая реплика с номером turn_id. Заговорил во время ответа — синтез
останавливается, очередь звука у клиента сбрасывается, номер растёт (перебивание). Промежуточная
расшифровка уходит в диалог с is_final=False: на красный флаг ассистент отвечает, не дожидаясь
конца фразы. Звук и текст разговора шлюз не сохраняет и не пишет в журнал.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from app.dialogue.models import Turn
from app.voice.endpoint import FRAME_BYTES, Endpointer, EndpointSettings
from app.voice.render import Renderer, default_renderer
from app.voice.settings import VoiceSettings

log = logging.getLogger(__name__)

# Пауза между склеенными фразами (192 мс), если пациент договорил после того, как шлюз решил,
# что он закончил
GLUE = bytes(FRAME_BYTES * 6)
# Ответ с такими словами ждёт диктовки: ФИО, дата рождения, номер
DICTATION = re.compile(r"фамили|дат\w* рождения|полис|номер телефона|продиктуйте", re.I)
# Клиент начинает играть звук чуть позже, чем шлюз его отправил
PLAYBACK_MARGIN_S = 0.3


class VadStream(Protocol):
    def probability(self, frame: bytes) -> float: ...


class Recognizer(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


class Synthesizer(Protocol):
    def synthesize(self, speech: str) -> tuple[bytes, int]: ...


class Dialogue(Protocol):
    async def handle(self, call_id: str, turn: Turn) -> object: ...

    async def end(self, call_id: str) -> object: ...


class Channel(Protocol):
    async def send_json(self, message: dict) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...


class CallSession:
    def __init__(
        self,
        call_id: str,
        channel: Channel,
        dialogue: Dialogue,
        vad: VadStream,
        recognizer: Recognizer,
        synthesizer: Synthesizer,
        *,
        today: Callable[[], date],
        settings: VoiceSettings | None = None,
        renderer: Renderer | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self.call_id = call_id
        self.channel = channel
        self.dialogue = dialogue
        self.vad = vad
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.today = today
        self.settings = settings or VoiceSettings()
        self.renderer = renderer or default_renderer()
        self.now = now
        s = self.settings
        self.endpointer = Endpointer(
            EndpointSettings(
                speech_threshold=s.speech_threshold,
                silence_threshold=s.silence_threshold,
                min_speech_ms=s.min_speech_ms,
                end_silence_ms=s.end_silence_ms,
                max_phrase_ms=s.max_phrase_ms,
            )
        )
        self.turn_id = 0
        self.ended = False
        # Решено завершить или перевести: новые фразы в диалог уже не идут
        self.closing = False
        self.finished = asyncio.Event()
        self.timings: list[dict] = []
        self._buffer = bytearray()
        self._playback_end = 0.0
        self._interruptible = True
        # Последняя реплика, которую уже получил диалог: её не склеиваем со следующей
        self._asked_turn = 0
        self._last_reply: list[str] = []
        self._barged_in = False
        self._answered: set[int] = set()
        self._next_partial_ms = s.partial_ms
        self._dialog_lock = asyncio.Lock()
        self._responder: asyncio.Task | None = None
        self._responder_audio = b""
        self._carry_audio = b""
        self._partial: asyncio.Task | None = None
        self._watch: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.channel.send_json({"type": "ready", "call_id": self.call_id})
        today = self.today()
        await self._say(0, [self.renderer.render("greeting.inbound", today).text])
        # Если приветствие перебили шумом, переспросим коротко, а не повторим его целиком
        self._last_reply = [self.renderer.render("ask.how_can_help", today).text]
        self._watch_silence()

    async def feed(self, pcm: bytes) -> None:
        """Звук от клиента: PCM 16 бит, 16 кГц, моно, кусками любой длины."""
        if self.ended:
            return
        self._buffer.extend(pcm)
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            event = self.endpointer.push(frame, self.vad.probability(frame))
            if event == "start":
                await self._phrase_started()
            elif event == "end":
                self._phrase_ended(self.endpointer.take())
            elif self.endpointer.in_phrase and self.endpointer.phrase_ms >= self._next_partial_ms:
                self._next_partial_ms += self.settings.partial_ms
                self._start_partial()

    def speaking(self) -> bool:
        return self.now() < self._playback_end + PLAYBACK_MARGIN_S

    async def close(self) -> None:
        self.ended = True
        for task in [self._responder, self._partial, self._watch, *self._tasks]:
            if task is not None and not task.done():
                task.cancel()
        try:
            await self.dialogue.end(self.call_id)
        except Exception:
            log.exception("диалог не завершил звонок")

    async def _phrase_started(self) -> None:
        self._cancel(self._watch)
        self.turn_id += 1
        self._next_partial_ms = self.settings.partial_ms
        self._barged_in = False
        if not self._interruptible and self.speaking():
            # Экстренную реплику и перевод не обрываем
            return
        if self.speaking():
            self._barged_in = True
            self._playback_end = 0.0
            await self.channel.send_json({"type": "stop", "turn_id": self.turn_id - 1})
        if self._responder is not None and not self._responder.done():
            if self._asked_turn != self.turn_id - 1:
                # Прошлая фраза ещё распознаётся — пациент договаривает её же. Если диалог её
                # уже получил, это новая реплика: склейка повторила бы прошлую в следующей.
                self._carry_audio = self._responder_audio
            self._responder.cancel()

    def _phrase_ended(self, audio: bytes) -> None:
        if self.closing:
            return
        if self._carry_audio:
            audio = self._carry_audio + GLUE + audio
            self._carry_audio = b""
        self._responder_audio = audio
        self._responder = asyncio.create_task(
            self._respond(self.turn_id, audio, self.now(), self.endpointer.end_silence_ms)
        )

    async def _respond(self, turn: int, audio: bytes, detected: float, silence: int) -> None:
        text = await self._transcribe(audio)
        recognized = self.now()
        await self.channel.send_json(
            {"type": "transcript", "turn_id": turn, "text": text, "final": True}
        )
        if turn in self._answered:
            return
        if not text:
            # Перебили шумом или кашлем — повторяем вопрос, иначе разговор повиснет
            if self._barged_in and self._last_reply:
                await self._say(turn, self._last_reply[-1:])
                self._watch_silence()
            return
        self._asked_turn = turn
        reply = await self._ask(turn, text)
        if turn in self._answered:
            return
        answered = self.now()
        timing = {
            "turn_id": turn,
            # Шлюз ждал столько тишины, чтобы понять, что фраза кончилась
            "silence_ms": silence,
            "asr_ms": round((recognized - detected) * 1000),
            "dialog_ms": round((answered - recognized) * 1000),
        }
        await self._deliver(turn, reply, timing, detected)

    async def _ask(self, turn: int, text: str) -> object | None:
        request = asyncio.ensure_future(self._handle(turn, text, final=True))
        done, _ = await asyncio.wait({request}, timeout=self.settings.wait_phrase_s)
        if not done:
            wait = self.renderer.render("wait.schedule", self.today()).text
            await self._say(turn, [wait])
        # Ход диалога не отменяем: прерванная обработка оставила бы реплику незавершённой
        return await asyncio.shield(request)

    async def _handle(self, turn: int, text: str, final: bool) -> object | None:
        async with self._dialog_lock:
            try:
                return await self.dialogue.handle(
                    self.call_id, Turn(turn_id=turn, text=text, is_final=final)
                )
            except Exception:
                log.exception("диалог не ответил на реплику %s", turn)
                return None

    async def _deliver(
        self, turn: int, reply: object | None, timing: dict | None = None, detected: float = 0.0
    ) -> None:
        # Уже переводим или прощаемся — второй ответ не нужен
        if turn != self.turn_id or self.ended or self.closing:
            return
        if reply is None:
            # Раздел 7.3, R-06: система не отвечает — к регистратору
            text = self.renderer.render("transfer.mis_down", self.today()).text
            self.closing = True
            await self._say(turn, [text], interruptible=False)
            self._spawn(self._finish("transfer", {"route": "R-06", "priority": "P3"}))
            return
        utterances = list(getattr(reply, "utterances", ()))
        transfer = getattr(reply, "transfer", None)
        phase = str(getattr(reply, "phase", "") or "")
        code = getattr(reply, "code", None)
        await self.channel.send_json(
            {
                "type": "reply",
                "turn_id": turn,
                "utterances": utterances,
                "code": code,
                "phase": phase,
            }
        )
        self._last_reply = utterances
        self._interruptible = transfer is None
        self.closing = transfer is not None or phase == "ended" or code == "CALL_ENDED"
        dictation = getattr(reply, "expect", None) in {"full_name", "birth_date"} or any(
            DICTATION.search(u) for u in utterances
        )
        self.endpointer.end_silence_ms = (
            self.settings.dictation_silence_ms if dictation else self.settings.end_silence_ms
        )
        await self._say(turn, utterances, timing, detected, interruptible=self._interruptible)
        if transfer is not None:
            route = getattr(transfer, "route", None)
            priority = getattr(transfer, "priority", None)
            self._spawn(self._finish("transfer", {"route": route, "priority": priority}))
        elif self.closing:
            self._spawn(self._finish("ended", delay=self.settings.goodbye_hangup_s))
        else:
            self._watch_silence()

    async def _say(
        self,
        turn: int,
        utterances: Sequence[str],
        timing: dict | None = None,
        detected: float = 0.0,
        interruptible: bool = True,
    ) -> None:
        for text in utterances:
            if self.ended or (interruptible and turn != self.turn_id):
                return
            started = self.now()
            speech = self.renderer.speak(text, self.today())
            pcm, rate = await asyncio.to_thread(self.synthesizer.synthesize, speech)
            if self.ended or (interruptible and turn != self.turn_id):
                return
            await self.channel.send_json(
                {"type": "speak", "turn_id": turn, "text": text, "sample_rate": rate}
            )
            await self.channel.send_bytes(pcm)
            self._playback_end = max(self.now(), self._playback_end) + len(pcm) / 2 / rate
            if timing is not None:
                timing["tts_ms"] = round((self.now() - started) * 1000)
                timing["total_ms"] = round((self.now() - detected) * 1000)
                timing["since_speech_ms"] = timing["total_ms"] + timing["silence_ms"]
                self.timings.append(timing)
                await self.channel.send_json({"type": "timing", **timing})
                timing = None

    def _start_partial(self) -> None:
        if self._partial is not None and not self._partial.done():
            return
        self._partial = asyncio.create_task(
            self._partial_turn(self.turn_id, self.endpointer.audio())
        )

    async def _partial_turn(self, turn: int, audio: bytes) -> None:
        text = await self._transcribe(audio)
        if not text or turn != self.turn_id or turn in self._answered:
            return
        await self.channel.send_json(
            {"type": "transcript", "turn_id": turn, "text": text, "final": False}
        )
        if self._dialog_lock.locked() or self.closing:
            return
        reply = await self._handle(turn, text, final=False)
        if reply is None or getattr(reply, "kind", "reply") == "partial" or turn in self._answered:
            return
        # Диалог ответил, не дожидаясь конца фразы: красный флаг или перевод
        self._answered.add(turn)
        await self._deliver(turn, reply)

    async def _transcribe(self, audio: bytes) -> str:
        try:
            return await asyncio.to_thread(self.recognizer.transcribe, audio)
        except Exception:
            log.exception("распознавание не удалось")
            return ""

    def _spawn(self, coroutine) -> None:
        # Завершение звонка не должна отменить новая фраза пациента
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _watch_silence(self) -> None:
        self._cancel(self._watch)
        self._watch = asyncio.create_task(self._silence())

    async def _silence(self) -> None:
        for template in ("silence.first", "silence.final"):
            await self._sleep_until(self._playback_end + self.settings.silence_prompt_s)
            if self.endpointer.in_phrase or self.ended:
                return
            text = self.renderer.render(template, self.today()).text
            await self._say(self.turn_id, [text])
        self.closing = True
        await self._finish("silence")

    async def _finish(self, reason: str, extra: dict | None = None, delay: float = 0.0) -> None:
        await self._sleep_until(self._playback_end + delay)
        if self.ended:
            return
        self.ended = True
        await self.channel.send_json({"type": "end", "reason": reason, **(extra or {})})
        self.finished.set()

    async def _sleep_until(self, moment: float) -> None:
        while (left := moment - self.now()) > 0:
            await asyncio.sleep(min(left, 0.25))

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()
