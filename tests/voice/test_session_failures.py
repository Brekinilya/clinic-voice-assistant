"""T03: ни пустой результат, ни сбой движка не оставляют звонок без следующего шага."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.voice.api import open_session
from app.voice.fallback import load_fallback
from app.voice.work import SpeechRuntime
from tests.voice.fakes import SPEECH, Channel, Dialogue, Recognizer, Synthesizer, Vad, phrase, reply
from tests.voice.test_session import answered, session, until


async def test_empty_asr_without_interruption_reasks_and_restarts_the_timer():
    call, channel = session(
        Dialogue(lambda turn: reply("Не должен прозвучать")), Recognizer({}), partial_ms=5000
    )
    try:
        await answered(call)
        await call.feed(phrase(10))
        await until(lambda: len(channel.said()) > 1, timeout=0.6)
        assert "не удалось разобрать" in channel.said()[-1]
        assert call._watch is not None and not call._watch.done()
        assert not call.dialogue.turns
    finally:
        await call.close()


async def test_three_empty_recognitions_request_the_operator():
    call, channel = session(
        Dialogue(lambda turn: reply("Не должен прозвучать")), Recognizer({}), partial_ms=5000
    )
    try:
        await answered(call)
        for number in range(1, 4):
            await call.feed(phrase(10))
            await until(lambda: len(channel.of("transcript")) >= number)
            await until(lambda: not call.speaking())
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["route"] == "R-05"
        assert not call.dialogue.turns
    finally:
        await call.close()


async def test_asr_exception_has_a_bounded_failure_route():
    class FailedRecognizer(Recognizer):
        def transcribe(self, pcm):
            raise RuntimeError("внутренняя ошибка движка")

    call, channel = session(
        Dialogue(lambda turn: reply("Не должен прозвучать")), FailedRecognizer({}), partial_ms=5000
    )
    try:
        await answered(call)
        await call.feed(phrase(10))
        await until(lambda: call.closing, timeout=0.6)
        assert channel.of("transcript")[-1]["status"] == "failed"
        assert not call.dialogue.turns
    finally:
        await call.close()


async def test_late_recognition_does_not_reach_a_new_turn():
    call, channel = session(
        Dialogue(lambda turn: reply("Устаревший ответ")),
        Recognizer({10: "Да"}, delay=0.1),
        partial_ms=5000,
    )
    try:
        await answered(call)
        call.turn_id = 1
        task = asyncio.create_task(call._respond(1, phrase(10), call.now(), 320))
        await asyncio.sleep(0.02)
        call.turn_id = 2
        await task
        assert not call.dialogue.turns
        assert not channel.of("transcript")
    finally:
        await call.close()


async def test_asr_timeout_does_not_wait_for_the_native_thread():
    call, channel = session(
        Dialogue(lambda turn: reply("Не должен прозвучать")),
        Recognizer({10: "Да"}, delay=0.2),
        partial_ms=5000,
        asr_timeout_s=0.03,
    )
    try:
        await answered(call)
        started = time.monotonic()
        await call.feed(phrase(10))
        await until(lambda: call.closing, timeout=0.15)
        assert time.monotonic() - started < 0.2
        assert not call.dialogue.turns
    finally:
        await call.close()


async def test_tts_exception_plays_prepared_audio_without_retrying_the_model():
    class FailedSynthesizer(Synthesizer):
        def synthesize(self, speech):
            raise RuntimeError("синтез недоступен")

    call, channel = session(
        Dialogue(lambda turn: reply("Ответ")), Recognizer({}), FailedSynthesizer()
    )
    try:
        await call.start()
        assert call.closing
        assert channel.audio and channel.of("speak")[-1]["fallback"] is True
    finally:
        await call.close()


async def offer_operator_after_empty_recognition(call, channel):
    """Ждём обработки, а не только transcript: он публикуется до ответа и синтеза."""
    await answered(call)
    for count in (1, 2):
        await call.feed(phrase(10))
        await until(lambda: len(channel.of("transcript")) >= count)
        # Иначе следующая фраза попадёт в ещё работающий responder и склеится с прежней.
        await until(lambda: call._responder is not None and call._responder.done())
        await until(lambda: not call.speaking())


async def test_yes_to_the_operator_offer_is_never_a_booking_confirmation():
    dialogue = Dialogue(lambda turn: reply("Вы записаны"))
    call, channel = session(dialogue, Recognizer({9: "Да"}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        assert call._operator_offered
        await call.feed(phrase(9))
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["route"] == "R-05"
        assert not dialogue.turns and "Вы записаны" not in channel.said()
    finally:
        await call.close()


async def test_emergency_preempts_the_local_operator_offer():
    def answer(turn):
        if "груди" in turn.text:
            return reply("Звоните сто три.", transfer=SimpleNamespace(route="R-01", priority="P1"))
        return reply("Ожидаю", kind="partial")

    call, channel = session(
        Dialogue(answer, urgent=True), Recognizer({9: "Да, давит в груди"}), partial_ms=5000
    )
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await call.feed(phrase(9))
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["route"] == "R-01"
    finally:
        await call.close()


async def test_dialogue_timeout_stops_automation_and_ignores_the_late_answer():
    release = asyncio.Event()

    class SlowDialogue(Dialogue):
        async def handle(self, call_id, turn):
            if turn.is_final:
                await release.wait()
                return reply("Поздний ответ")
            return reply("Ожидаю", kind="partial")

    dialogue = SlowDialogue(None)
    call, channel = session(
        dialogue,
        Recognizer({10: "Да"}),
        partial_ms=5000,
        wait_phrase_s=0.01,
        dialogue_timeout_s=0.04,
    )
    try:
        await answered(call)
        await call.feed(phrase(10))
        await until(lambda: call.closing, timeout=0.5)
        assert dialogue.ended
        release.set()
        await asyncio.sleep(0.03)
        assert "Поздний ответ" not in channel.said()
    finally:
        release.set()
        await call.close()


async def test_tts_timeout_uses_audio_without_waiting_for_synthesis():
    class SlowSynthesizer(Synthesizer):
        def synthesize(self, speech):
            time.sleep(0.2)
            return super().synthesize(speech)

    call, channel = session(Dialogue(None), Recognizer({}), SlowSynthesizer(), tts_timeout_s=0.03)
    try:
        started = time.monotonic()
        await call.start()
        assert time.monotonic() - started < 0.15
        assert call.closing and channel.of("speak")[-1]["fallback"]
    finally:
        await call.close()


async def test_failing_tts_preserves_the_emergency_route_and_emergency_audio():
    class SelectiveSynthesizer(Synthesizer):
        broken = False

        def synthesize(self, speech):
            if self.broken:
                raise RuntimeError("failed")
            return super().synthesize(speech)

    synthesizer = SelectiveSynthesizer()
    call, channel = session(Dialogue(None), Recognizer({}), synthesizer)
    try:
        await answered(call)
        synthesizer.broken = True
        call.turn_id = 1
        await call._deliver(
            1, reply("Экстренная реплика", transfer=SimpleNamespace(route="R-01", priority="P1"))
        )
        fallback = channel.of("speak")[-1]
        assert fallback["fallback"] and fallback["text"] == load_fallback(True).text
        await until(lambda: channel.of("end"), timeout=6)
        assert channel.of("end")[-1]["route"] == "R-01"
    finally:
        await call.close()


@pytest.mark.parametrize("emergency", [False, True])
def test_prepared_audio_exists_without_loading_any_model(emergency):
    clip = load_fallback(emergency)
    assert clip.text and clip.pcm and clip.rate == 16000


async def test_oversized_audio_packet_is_rejected_before_buffering():
    call, channel = session(Dialogue(None), Recognizer({}))
    try:
        await call.start()
        await call.feed(bytes(call.settings.max_audio_chunk_bytes + 1))
        assert call.closing and not call._buffer
        assert channel.of("speak")[-1]["code"] == "audio_overflow"
        assert not call.dialogue.turns
    finally:
        await call.close()


async def test_empty_recognition_rearms_silence_until_the_call_finishes():
    call, channel = session(Dialogue(None), Recognizer({}), partial_ms=5000)
    try:
        await answered(call)
        call.settings = call.settings.model_copy(update={"silence_prompt_s": 0.03})
        await call.feed(phrase(10))
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["reason"] == "silence"
        assert "Вас не слышно. Перезвоните, пожалуйста." in channel.said()
    finally:
        await call.close()


async def test_declining_operator_does_not_confirm_the_old_action():
    dialogue = Dialogue(lambda turn: reply("Вы записаны"))
    call, channel = session(dialogue, Recognizer({9: "Нет"}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await call.feed(phrase(9))
        await until(lambda: not call._operator_offered)
        await until(lambda: call._watch is not None and not call._watch.done())
        assert not dialogue.turns and not call.closing
    finally:
        await call.close()


async def test_hangup_during_asr_discards_the_late_result():
    dialogue = Dialogue(lambda turn: reply("Поздний ответ"))
    call, channel = session(dialogue, Recognizer({10: "Да"}, delay=0.1), partial_ms=5000)
    await answered(call)
    await call.feed(phrase(10))
    await call.close()
    await asyncio.sleep(0.15)
    assert not dialogue.turns and not channel.of("transcript")
    assert "Поздний ответ" not in channel.said()


async def test_wait_phrase_cannot_extend_a_shorter_dialogue_deadline():
    release = asyncio.Event()

    class NeverAnswers(Dialogue):
        async def handle(self, call_id, turn):
            await release.wait()
            return reply("Поздний ответ")

    call, channel = session(
        NeverAnswers(None),
        Recognizer({10: "Да"}),
        partial_ms=5000,
        wait_phrase_s=5,
        dialogue_timeout_s=0.03,
    )
    try:
        await answered(call)
        await call.feed(phrase(10))
        await until(lambda: call.closing, timeout=0.2)
        assert call.dialogue.ended
    finally:
        release.set()
        await call.close()


async def test_application_sessions_share_the_same_model_queues():
    runtime = SpeechRuntime()
    state = SimpleNamespace(
        voice=SimpleNamespace(
            vad=SimpleNamespace(stream=Vad),
            recognizer=Recognizer({10: "Запишите"}),
            synthesizer=Synthesizer(),
        ),
        voice_runtime=runtime,
        dialogue=Dialogue(lambda turn: reply("Ответ")),
    )
    first, second = (
        open_session(state, "first", Channel()),
        open_session(state, "second", Channel()),
    )
    try:
        assert first.runtime is second.runtime is runtime
        await first.close()
        await second.start()
        assert not second.closing
        assert await runtime.asr.run(lambda: "ok", timeout=0.5) == "ok"
    finally:
        await second.close()
        runtime.close()


async def test_oversized_glued_phrase_is_not_truncated_or_sent_to_asr():
    recognizer = Recognizer({})
    call, channel = session(Dialogue(None), recognizer)
    try:
        await answered(call)
        call.turn_id = 1
        call._carry_audio = bytes((call.settings.max_phrase_ms + 320) * 32)
        call._phrase_ended(phrase(10))
        await until(lambda: len(channel.said()) > 1)
        assert not recognizer.heard and not call._carry_audio and not call._responder_audio
        assert "не удалось разобрать" in channel.said()[-1]
    finally:
        await call.close()


async def test_missing_audio_packets_mid_phrase_have_a_wall_clock_deadline():
    recognizer = Recognizer({8: "Да"})
    call, channel = session(
        Dialogue(lambda turn: reply("Недопустимый ответ")),
        recognizer,
        max_phrase_ms=1000,
        partial_ms=5000,
    )
    try:
        await answered(call)
        await call.feed(SPEECH * 8)
        assert call.endpointer.in_phrase
        # Больше не присылаем ни речь, ни тишину. Нельзя молча использовать оборванное «да».
        await until(lambda: len(channel.said()) > 1, timeout=1.6)
        assert not call.endpointer.in_phrase
        assert "не удалось разобрать" in channel.said()[-1]
        assert not recognizer.heard and not call.dialogue.turns
        assert call._watch is not None and not call._watch.done()
    finally:
        await call.close()
