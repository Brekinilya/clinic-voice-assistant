"""Один звонок через голосовой шлюз: фраза пациента → распознавание → диалог → синтез.

Каждая фраза пациента — новая реплика с номером turn_id. Заговорил во время ответа — синтез
останавливается, очередь звука у клиента сбрасывается, номер растёт (перебивание). Промежуточная
расшифровка уходит в диалог с is_final=False: на красный флаг ассистент отвечает, не дожидаясь
конца фразы. Звук не сохраняется; тексты доступны только в явно включённой диагностике.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from types import SimpleNamespace
from typing import Protocol

from app.diagnostics.channel import ObservedChannel
from app.dialogue.models import Turn
from app.dialogue.outbound import OutboundEvent
from app.nlu.parsers.answers import parse_answer
from app.voice.audibility import split_audibility
from app.voice.endpoint import FRAME_BYTES, Endpointer, EndpointSettings
from app.voice.fallback import load_fallback
from app.voice.render import Renderer, default_renderer
from app.voice.settings import VoiceSettings
from app.voice.work import InferenceBusy, SpeechRuntime

log = logging.getLogger(__name__)

# Пауза между склеенными фразами (192 мс), если пациент договорил после того, как шлюз решил,
# что он закончил
GLUE = bytes(FRAME_BYTES * 6)
# Ответ с такими словами ждёт диктовки: ФИО, дата рождения, номер
DICTATION = re.compile(r"фамили|дат\w* рождения|полис|номер телефона|продиктуйте", re.I)
# Клиент начинает играть звук чуть позже, чем шлюз его отправил
PLAYBACK_MARGIN_S = 0.3
# Столько сбоев распознавания подряд допустимо: дальше это неисправность, а не шум на линии
ASR_FAILURES_LIMIT = 2
# После такого ответа продолжать разговор нельзя: квитанция прошлой реплики не закрыта
INTERRUPTED = "PROCESSING_INTERRUPTED"


class RecognitionStatus(StrEnum):
    TEXT = "text"
    SILENCE = "silence"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALE = "stale"


@dataclass(frozen=True)
class Transcription:
    status: RecognitionStatus
    text: str = ""


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
        runtime: SpeechRuntime | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        outbound: object | None = None,
        machine: bool = False,
        on_outcome: Callable[[str | None, str | None], Awaitable[None]] | None = None,
    ):
        self.call_id = call_id
        # Исходящее напоминание: первым говорит диалог, исход С-10 уходит исполнителю
        self.outbound = outbound
        self.machine = machine
        self._on_outcome = on_outcome
        self._outcome: str | None = None
        self._mis_status: str | None = None
        self._outcome_sent = False
        debug = getattr(dialogue, "debug", None)
        self.channel = ObservedChannel(channel, debug, call_id) if debug is not None else channel
        self.dialogue = dialogue
        self.vad = vad
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.today = today
        self.settings = settings or VoiceSettings()
        self.renderer = renderer or default_renderer()
        self.now = now
        self.wall = wall
        # Диалог попросил подождать МИС: до этого момента тишина звонящего ожидаема.
        self._resume_at: float | None = None
        self._owns_runtime = runtime is None
        self.runtime = runtime or SpeechRuntime(
            self.settings.inference_queue_capacity, self.settings.dialogue_job_capacity
        )
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
        self._speech_deadline: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._end_request: asyncio.Task | None = None
        # Непонимание подряд: пустой ASR и NOT_UNDERSTOOD диалога считаются вместе (R-05)
        self._misunderstandings = 0
        self._asr_failures = 0
        self._operator_offered = False
        self._audibility_pending = False
        self._question_before_operator: list[str] = []
        self._pending_transfer: dict | None = None
        self._fallback_used = False
        # Почему разговор закончился переводом или выходом — для панели, без текстов
        self._cause: str | None = None

    async def start(self) -> None:
        if self.outbound is not None:
            await self._start_outbound()
            return
        await self.channel.send_json({"type": "ready", "call_id": self.call_id})
        today = self.today()
        if not await self._say(0, [self.renderer.render("greeting.inbound", today).text]):
            return
        # Если приветствие перебили шумом, переспросим коротко, а не повторим его целиком
        self._last_reply = [self.renderer.render("ask.how_can_help", today).text]
        self._watch_silence()

    async def _start_outbound(self) -> None:
        await self.channel.send_json(
            {"type": "ready", "call_id": self.call_id, "direction": "outbound"}
        )
        # Открытие — реплика 1, первая речь пациента получит номер 2 (app/dialogue/outbound)
        self.turn_id = 1
        try:
            reply = await asyncio.wait_for(
                self.dialogue.open_outbound(self.call_id, self.outbound),
                self.settings.dialogue_timeout_s,
            )
            if self.machine:
                # Автоответчику приветствие с именем не звучит: только нейтральное сообщение
                self.turn_id = 2
                event = OutboundEvent(
                    attempt_id=self.outbound.attempt_id, turn_id=2, kind="voicemail"
                )
                reply = await asyncio.wait_for(
                    self.dialogue.outbound_event(self.call_id, event),
                    self.settings.dialogue_timeout_s,
                )
        except Exception:
            # Пациент взял трубку, а диалог не начался: молча кладём, попытка без исхода
            log.error("диалог не открыл исходящий звонок")
            self._cause = "outbound_unavailable"
            self.closing = True
            self._spawn(self._finish("ended"))
            return
        await self._deliver(self.turn_id, reply)

    async def feed(self, pcm: bytes) -> None:
        """Звук от клиента: PCM 16 бит, 16 кГц, моно, кусками любой длины."""
        if self.ended or self.closing:
            return
        if len(pcm) > self.settings.max_audio_chunk_bytes:
            await self._voice_failure("audio_overflow")
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

    async def resume_after_transfer(self, outcome: str) -> bool:
        """Resume the caller dialogue after an unsuccessful P2/P3 operator attempt."""
        start_callback = getattr(self.dialogue, "start_callback", None)
        if start_callback is None:
            return False
        self.ended = False
        self.closing = False
        self.finished.clear()
        self.turn_id += 1
        self._answered.clear()
        self._fallback_used = False
        self._cause = None
        self._buffer.clear()
        self.endpointer = Endpointer(self.endpointer.settings)
        self._playback_end = self.now()
        result = await start_callback(self.call_id, self.turn_id, outcome)
        if result is None:
            self.ended = True
            self.finished.set()
            return False
        await self._deliver(self.turn_id, result)
        return True

    def speaking(self) -> bool:
        return self.now() < self._playback_end + PLAYBACK_MARGIN_S

    async def close(self) -> None:
        if not self.ended and getattr(self.dialogue, "debug", None) is not None:
            self.dialogue.debug.emit(self.call_id, None, "voice.closed", reason="channel_closed")
        self.ended = True
        self.finished.set()
        cancelled = []
        for task in [
            self._responder,
            self._partial,
            self._watch,
            self._speech_deadline,
            *self._tasks,
        ]:
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                cancelled.append(task)
        await asyncio.gather(*cancelled, return_exceptions=True)
        await self._end_dialogue()
        await self._report_outcome()
        if self._owns_runtime:
            self.runtime.close(cancel_requests=False)

    async def _report_outcome(self) -> None:
        if self._on_outcome is None or self._outcome_sent:
            return
        self._outcome_sent = True
        try:
            await self._on_outcome(self._outcome, self._mis_status)
        except Exception:
            log.error("не удалось сообщить исход напоминания")

    async def _end_dialogue(self) -> None:
        if self._end_request is None:
            self._end_request = asyncio.create_task(self.dialogue.end(self.call_id))
            self._end_request.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
        done, _ = await asyncio.wait(
            {self._end_request}, timeout=self.settings.input_started_timeout_s
        )
        if done and not self._end_request.cancelled() and self._end_request.exception():
            log.error("не удалось завершить состояние диалога")

    async def _phrase_started(self) -> None:
        if self.ended or self.closing:
            return
        self._cancel(self._watch)
        self._cancel(self._partial)
        self._cancel(self._speech_deadline)
        self._resume_at = None
        # Причина сбоя вытесненной реплики к новой не относится
        self._cause = None
        self.turn_id += 1
        try:
            # До ASR и вне очереди итоговых реплик отзываем прежнее «да».
            await asyncio.wait_for(
                self.dialogue.input_started(self.call_id, self.turn_id),
                timeout=self.settings.input_started_timeout_s,
            )
        except Exception:
            log.error("диалог не подтвердил начало новой речи")
            await self._deliver(self.turn_id, None, cause="input_started")
            return
        self._next_partial_ms = self.settings.partial_ms
        self._speech_deadline = asyncio.create_task(self._speech_timeout(self.turn_id))
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
        self._cancel(self._speech_deadline)
        if self._carry_audio:
            audio = self._carry_audio + GLUE + audio
            self._carry_audio = b""
        if len(audio) > (self.settings.max_phrase_ms + 320) * 32:
            # Длинную склейку не обрезаем: в потерянной части могло быть отрицание.
            self._responder_audio = b""
            self._responder = asyncio.create_task(self._unheard(self.turn_id))
            return
        self._responder_audio = audio
        self._responder = asyncio.create_task(
            self._respond(self.turn_id, audio, self.now(), self.endpointer.end_silence_ms)
        )

    async def _speech_timeout(self, turn: int) -> None:
        # Endpointer считает кадры. Этот независимый таймер нужен, если RTP/браузер
        # перестал присылать даже тишину и конца фразы по кадрам уже не будет.
        await asyncio.sleep(self.settings.max_phrase_ms / 1000)
        if self.ended or self.closing or turn != self.turn_id or not self.endpointer.in_phrase:
            return
        self._answered.add(turn)
        self._cancel(self._partial)
        previous = self.endpointer
        self.endpointer = Endpointer(previous.settings)
        self.endpointer.end_silence_ms = previous.end_silence_ms
        self._buffer.clear()
        self._carry_audio = self._responder_audio = b""
        await self._unheard(turn)

    async def _respond(self, turn: int, audio: bytes, detected: float, silence: int) -> None:
        result = await self._transcribe(audio, turn=turn)
        if result.status == RecognitionStatus.STALE:
            return
        text = result.text
        recognized = self.now()
        await self.channel.send_json(
            {
                "type": "transcript",
                "turn_id": turn,
                "text": text,
                "final": True,
                "status": result.status,
            }
        )
        if turn in self._answered:
            return
        if result.status == RecognitionStatus.TIMED_OUT:
            # Нативный поток модели всё ещё занят: переспрос лишь спрятал бы зависание
            await self._deliver(turn, None, cause="asr_timeout")
            return
        if result.status == RecognitionStatus.FAILED:
            await self._asr_failed(turn)
            return
        self._asr_failures = 0
        if result.status == RecognitionStatus.SILENCE:
            await self._unheard(turn)
            return
        # Проверка промежуточного текста этой же фразы должна закончиться до
        # итогового согласия. Иначе поздний partial меняет input_epoch уже после
        # authorize и прерывает запись. Части новой фразы по-прежнему идут сразу.
        partial = self._partial
        if partial is not None and not partial.done():
            done, _ = await asyncio.wait(
                {partial},
                timeout=self.settings.asr_timeout_s + self.settings.dialogue_timeout_s,
            )
            if not done:
                await self._deliver(turn, None, cause="partial_timeout")
                return
        if turn != self.turn_id or self.ended or self.closing or turn in self._answered:
            return
        if self._audibility_pending or self._operator_offered:
            # Ответ на вопрос шлюза не подтверждает старую запись.
            # Экстренный маршрут всё равно проверяется первым.
            safety = await self._handle(turn, text, final=False)
            if turn != self.turn_id or self.ended or self.closing or turn in self._answered:
                return
            if safety is None:
                await self._deliver(turn, None, cause="dialogue_unavailable")
                return
            if safety is not None and getattr(safety, "kind", "reply") != "partial":
                await self._deliver(turn, safety)
                return
        if self._audibility_pending:
            self._audibility_pending = False
            answer, rest = split_audibility(text)
            if answer is not None and rest:
                # «Да, слышу, отмените запись»: «да» ответило шлюзу, команда идёт в диалог
                text = rest
            elif answer is not None:
                # Ответили на вопрос шлюза, а не подтвердили прежнюю бронь.
                # Реплику нельзя склеивать со следующим ответом во время синтеза.
                # Счёт непонимания не трогаем: иначе тишина обнуляла бы путь к R-05.
                self._asked_turn = turn
                self._answered.add(turn)
                phrases = list(self._last_reply)
                if answer == "no":
                    phrases.insert(0, "Попробую повторить.")
                if await self._say(
                    turn,
                    phrases or [self.renderer.render("ask.how_can_help", self.today()).text],
                ):
                    self._watch_silence()
                return
        if self._operator_offered:
            answer = parse_answer(text)
            self._operator_offered = False
            if answer == "yes":
                await self._recognition_transfer(turn)
                return
            if answer == "no":
                # Отказ не обнуляет счёт: следующее непонимание — третье, и это перевод R-05.
                # Иначе предложение оператора повторялось бы по кругу.
                self._asked_turn = turn
                self._answered.add(turn)
                self._last_reply = self._question_before_operator or [
                    self.renderer.render("ask.how_can_help", self.today()).text
                ]
                if await self._say(turn, self._last_reply):
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
        try:
            request = self.runtime.request(self._handle(turn, text, final=True))
        except InferenceBusy:
            self._cause = "dialogue_busy"
            await self._end_dialogue()
            return None
        started = self.now()
        done, _ = await asyncio.wait(
            {request}, timeout=min(self.settings.wait_phrase_s, self.settings.dialogue_timeout_s)
        )
        if not done:
            wait = self.renderer.render("wait.schedule", self.today()).text
            if not await self._say(turn, [wait]):
                return None
            done, _ = await asyncio.wait(
                {request}, timeout=max(0, self.settings.dialogue_timeout_s - (self.now() - started))
            )
        if not done:
            self._cause = "dialogue_timeout"
            await self._end_dialogue()
            return None
        # Ход диалога не отменяем: прерванная обработка оставила бы реплику незавершённой
        return request.result()

    async def _handle(self, turn: int, text: str, final: bool) -> object | None:
        # Итоговые реплики — по очереди. Промежуточная идёт сразу, даже пока диалог занят прошлой
        # итоговой (ждёт МИС): по ней диалог проверяет safety и отзывает разрешение на запись
        if not final:
            return await self._ask_dialogue(turn, text, final)
        async with self._dialog_lock:
            return await self._ask_dialogue(turn, text, final)

    async def _ask_dialogue(self, turn: int, text: str, final: bool) -> object | None:
        try:
            operation = self.dialogue.handle(
                self.call_id, Turn(turn_id=turn, text=text, is_final=final)
            )
            return (
                await operation
                if final
                else await asyncio.wait_for(operation, self.settings.dialogue_timeout_s)
            )
        except Exception:
            log.error("диалог не ответил на реплику %s", turn)
            return None

    async def _recognition_transfer(self, turn: int) -> None:
        self._cause = "not_understood"
        await self._deliver(
            turn,
            SimpleNamespace(
                utterances=("Плохо слышно. Соединяю с сотрудником.",),
                code="ASR_NOT_UNDERSTOOD",
                transfer=SimpleNamespace(route="R-05", priority="P3"),
                phase="handoff",
            ),
        )

    async def _asr_failed(self, turn: int) -> None:
        # Сбой движка — не непонимание пациента: один переспрос, повторный сбой подряд — R-06
        self._asr_failures += 1
        if self._asr_failures >= ASR_FAILURES_LIMIT:
            await self._deliver(turn, None, cause="asr_failed")
            return
        self._asked_turn = turn
        self._answered.add(turn)
        if await self._say(turn, [self.renderer.render("reask.first", self.today()).text]):
            self._watch_silence()

    async def _offer_operator(self, turn: int) -> str:
        self._operator_offered = True
        self._question_before_operator = list(self._last_reply)
        await self.channel.send_json({"type": "question", "turn_id": turn, "kind": "operator"})
        return self.renderer.render("reask.offer_operator", self.today()).text

    async def _unheard(self, turn: int) -> None:
        if self.ended or self.closing or turn != self.turn_id:
            return
        self._misunderstandings += 1
        if self._misunderstandings >= 3:
            await self._recognition_transfer(turn)
            return
        if self._misunderstandings == 2:
            phrase = await self._offer_operator(turn)
            # На «Вы меня слышите?» повторяем именно этот вопрос
            self._last_reply = [phrase]
        elif self._barged_in and self._last_reply:
            phrase = self._last_reply[-1]
        else:
            phrase = self.renderer.render("reask.first", self.today()).text
        if await self._say(turn, [phrase]):
            self._watch_silence()

    async def _deliver(
        self,
        turn: int,
        reply: object | None,
        timing: dict | None = None,
        detected: float = 0.0,
        *,
        cause: str | None = None,
    ) -> None:
        # Уже переводим или прощаемся — второй ответ не нужен
        if turn != self.turn_id or self.ended or self.closing:
            return
        code = getattr(reply, "code", None)
        if reply is None or code == INTERRUPTED:
            # Раздел 7.3, R-06: система не отвечает — к регистратору. Прерванная обработка —
            # тоже тупик: на каждую следующую реплику диалог ответил бы тем же.
            if reply is not None:
                cause = "interrupted"
            self._cause = cause or self._cause or "dialogue_unavailable"
            text = self.renderer.render("transfer.mis_down", self.today()).text
            self.closing = True
            self._pending_transfer = {"route": "R-06", "priority": "P3"}
            await self._end_dialogue()
            if not await self._say(turn, [text], interruptible=False):
                return
            self._spawn(self._finish("transfer", {"route": "R-06", "priority": "P3"}))
            return
        utterances = list(getattr(reply, "utterances", ()))
        self._audibility_pending = False
        transfer = getattr(reply, "transfer", None)
        phase = str(getattr(reply, "phase", "") or "")
        result = getattr(reply, "reminder_result", None)
        if self.outbound is not None and result is not None:
            # Исход берём из результата диалога, а не из кода реплики: в нём и статус МИС
            if result.attempt_id == self.outbound.attempt_id:
                self._outcome, self._mis_status = result.outcome, result.mis_status
        if code == "NOT_UNDERSTOOD":
            self._misunderstandings += 1
            if self._misunderstandings >= 3:
                await self._recognition_transfer(turn)
                return
            if self._misunderstandings == 2:
                utterances = [await self._offer_operator(turn)]
        elif code != "ASR_NOT_UNDERSTOOD":
            self._misunderstandings = 0
        self._resume_at = self._mis_resume(reply) if code == "WAIT_MIS" else None
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
        if code == "ASR_NOT_UNDERSTOOD":
            # Сначала прекращаем приём новых фраз, затем отзываем право фонового действия.
            await self._end_dialogue()
        self._pending_transfer = (
            {
                "route": getattr(transfer, "route", None),
                "priority": getattr(transfer, "priority", None),
            }
            if transfer is not None
            else None
        )
        dictation = getattr(reply, "expect", None) in {"full_name", "birth_date"} or any(
            DICTATION.search(u) for u in utterances
        )
        self.endpointer.end_silence_ms = (
            self.settings.dictation_silence_ms if dictation else self.settings.end_silence_ms
        )
        if not utterances and not self.closing:
            await self._voice_failure("empty_reply")
            return
        if not await self._say(
            turn, utterances, timing, detected, interruptible=self._interruptible
        ):
            return
        if transfer is not None:
            route = getattr(transfer, "route", None)
            priority = getattr(transfer, "priority", None)
            self._cause = self._cause or "dialogue"
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
    ) -> bool:
        for text in utterances:
            if self.ended or (interruptible and turn != self.turn_id):
                return False
            started = self.now()
            speech = self.renderer.speak(text, self.today())
            try:
                pcm, rate = await self.runtime.tts.run(
                    self.synthesizer.synthesize, speech, timeout=self.settings.tts_timeout_s
                )
                if not pcm or rate <= 0:
                    raise ValueError("пустое аудио")
            except Exception:
                if not self.ended and turn == self.turn_id:
                    await self._voice_failure("tts_unavailable")
                return False
            if self.ended or (interruptible and turn != self.turn_id):
                return False
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
                del self.timings[:-128]
                await self.channel.send_json({"type": "timing", **timing})
                timing = None
        return True

    async def _voice_failure(self, code: str) -> None:
        if self.ended or self._fallback_used:
            return
        self._fallback_used = True
        self._cause = code
        self.closing = True
        self._interruptible = False
        self._cancel(self._watch)
        await self._end_dialogue()
        transfer = self._pending_transfer or {"route": "R-06", "priority": "P3"}
        clip = load_fallback(transfer["route"] in {"R-01", "R-11"})
        await self.channel.send_json({"type": "stop", "turn_id": self.turn_id})
        await self.channel.send_json(
            {
                "type": "speak",
                "turn_id": self.turn_id,
                "text": clip.text,
                "sample_rate": clip.rate,
                "fallback": True,
                "code": code,
            }
        )
        await self.channel.send_bytes(clip.pcm)
        self._playback_end = self.now() + len(clip.pcm) / 2 / clip.rate
        self._spawn(self._finish("transfer", transfer))

    def _start_partial(self) -> None:
        if self._partial is not None and not self._partial.done():
            return
        self._partial = asyncio.create_task(
            self._partial_turn(self.turn_id, self.endpointer.audio())
        )

    async def _partial_turn(self, turn: int, audio: bytes) -> None:
        result = await self._transcribe(audio, turn=turn, partial=True)
        text = result.text
        if not text or turn != self.turn_id or turn in self._answered:
            return
        await self.channel.send_json(
            {"type": "transcript", "turn_id": turn, "text": text, "final": False}
        )
        if self.closing:
            return
        reply = await self._handle(turn, text, final=False)
        if reply is None or getattr(reply, "kind", "reply") == "partial" or turn in self._answered:
            return
        # Диалог ответил, не дожидаясь конца фразы: красный флаг или перевод
        self._answered.add(turn)
        await self._deliver(turn, reply)

    async def _transcribe(self, audio: bytes, *, turn: int, partial: bool = False) -> Transcription:
        try:
            text = await self.runtime.asr.run(
                self.recognizer.transcribe,
                audio,
                timeout=self.settings.asr_timeout_s,
                priority=10 if partial else 0,
            )
            result = Transcription(
                RecognitionStatus.TEXT if text.strip() else RecognitionStatus.SILENCE, text.strip()
            )
        except TimeoutError:
            result = Transcription(RecognitionStatus.TIMED_OUT)
        except Exception:
            log.error("распознавание не удалось")
            result = Transcription(RecognitionStatus.FAILED)
        if turn != self.turn_id or self.ended or self.closing:
            return Transcription(RecognitionStatus.STALE)
        return result

    def _spawn(self, coroutine) -> None:
        # Завершение звонка не должна отменить новая фраза пациента
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _watch_silence(self) -> None:
        if self.ended or self.closing:
            return
        self._cancel(self._watch)
        self._watch = asyncio.create_task(self._silence())

    def _mis_resume(self, reply: object) -> float | None:
        retry_at = getattr(reply, "retry_at", None)
        if isinstance(retry_at, str):
            retry_at = datetime.fromisoformat(retry_at)
        if not isinstance(retry_at, datetime):
            return None
        # Дольше двух минут диалог ждать не просит: дальше он сам передаёт регистратору.
        delay = min(120.0, max(0.0, (retry_at - self.wall()).total_seconds()))
        return self.now() + delay

    async def _silence(self) -> None:
        resume, self._resume_at = self._resume_at, None
        if resume is not None:
            # Звонящего попросили подождать: не торопим его «Вы меня слышите?».
            await self._sleep_until(max(resume, self._playback_end))
            if self.endpointer.in_phrase or self.ended:
                return
            text = self.renderer.render("wait.mis_ready", self.today()).text
            if not await self._say(self.turn_id, [text]):
                return
        for template in ("silence.first", "silence.final"):
            await self._sleep_until(self._playback_end + self.settings.silence_prompt_s)
            if self.endpointer.in_phrase or self.ended:
                return
            text = self.renderer.render(template, self.today()).text
            if template == "silence.first":
                self._audibility_pending = True
                await self.channel.send_json(
                    {"type": "question", "turn_id": self.turn_id, "kind": "audibility"}
                )
            if not await self._say(self.turn_id, [text]):
                return
        self.closing = True
        await self._finish("silence")

    async def _finish(self, reason: str, extra: dict | None = None, delay: float = 0.0) -> None:
        await self._sleep_until(self._playback_end + delay)
        if self.ended:
            return
        self.ended = True
        cause = {"cause": self._cause} if self._cause else {}
        await self.channel.send_json({"type": "end", "reason": reason, **(extra or {}), **cause})
        self.finished.set()

    async def _sleep_until(self, moment: float) -> None:
        while (left := moment - self.now()) > 0:
            await asyncio.sleep(min(left, 0.25))

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
