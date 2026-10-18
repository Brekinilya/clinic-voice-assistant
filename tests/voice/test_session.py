"""Звонок через шлюз на подставных движках: без моделей, звук — кадры тишины и «речи»."""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.voice.endpoint import FRAME_BYTES
from app.voice.session import CallSession
from app.voice.settings import VoiceSettings
from tests.voice.fakes import (
    QUIET,
    SPEECH,
    Channel,
    Dialogue,
    Recognizer,
    Synthesizer,
    Vad,
    phrase,
    reply,
)

TODAY = date(2026, 9, 30)
SETTINGS = VoiceSettings(
    _env_file=None,
    min_speech_ms=192,
    end_silence_ms=320,
    dictation_silence_ms=640,
    partial_ms=320,
    silence_prompt_s=30,
    goodbye_hangup_s=0.05,
    wait_phrase_s=0.3,
)


def session(dialogue, recognizer, synthesizer=None, **settings):
    channel = Channel()
    call = CallSession(
        "call-1",
        channel,
        dialogue,
        Vad(),
        recognizer,
        synthesizer or Synthesizer(),
        today=lambda: TODAY,
        settings=SETTINGS.model_copy(update=settings),
    )
    return call, channel


async def answered(call):
    """Начать звонок и дождаться, пока прозвучит приветствие."""
    await call.start()
    await until(lambda: not call.speaking())


async def test_slow_input_started_fails_closed_without_waiting_for_backend():
    class SlowDialogue(Dialogue):
        async def input_started(self, call_id, turn_id):
            await asyncio.Event().wait()

    dialogue = SlowDialogue(lambda turn: reply("Не должен прозвучать"))
    call, channel = session(dialogue, Recognizer({10: "Да"}), input_started_timeout_s=0.03)
    await call.start()
    await asyncio.wait_for(call._phrase_started(), timeout=0.5)
    assert call.closing
    assert not dialogue.turns
    assert "Не должен прозвучать" not in channel.said()
    await call.close()


async def until(condition, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("не дождались")
        await asyncio.sleep(0.01)


async def test_greeting_then_answer_with_timing():
    dialogue = Dialogue(lambda turn: reply("Назовите, пожалуйста, фамилию, имя, отчество."))
    call, channel = session(dialogue, Recognizer({10: "Мне бы записаться к терапевту"}))
    await answered(call)
    assert channel.said()[0].startswith("Здравствуйте! Регистратура поликлиники номер семь")

    await call.feed(phrase(10))
    await until(lambda: len(channel.of("timing")) == 1)
    turn = dialogue.turns[-1]
    assert (turn.turn_id, turn.text, turn.is_final) == (1, "Мне бы записаться к терапевту", True)
    assert channel.said()[-1] == "Назовите, пожалуйста, фамилию, имя, отчество."
    assert len(channel.audio) == 2
    timing = channel.of("timing")[0]
    assert {"asr_ms", "dialog_ms", "tts_ms", "total_ms", "since_speech_ms"} <= set(timing)
    # ждём ФИО — паузы при диктовке длиннее
    assert call.endpointer.end_silence_ms == SETTINGS.dictation_silence_ms
    await call.close()
    assert dialogue.ended


async def test_patient_interrupts_answer():
    long_answer = "Есть завтра в девять тридцать или в пятницу в десять. Что удобнее?"
    dialogue = Dialogue(lambda turn: reply(long_answer if turn.turn_id == 1 else "Записываю."))
    synthesizer = Synthesizer({long_answer: 5.0})
    call, channel = session(
        dialogue, Recognizer({10: "К терапевту", 8: "Давайте завтра"}), synthesizer
    )
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: long_answer in channel.said())
    assert call.speaking()

    await call.feed(SPEECH * 8)
    assert channel.of("stop") == [{"type": "stop", "turn_id": 1}]
    assert call.turn_id == 2
    await call.feed(QUIET * 12)
    await until(lambda: "Записываю." in channel.said())
    assert [t.turn_id for t in dialogue.turns] == [1, 2]
    await call.close()


async def test_noise_interruption_repeats_question():
    question = "Записываю к терапевту на пятницу. Верно?"
    dialogue = Dialogue(lambda turn: reply(question))
    synthesizer = Synthesizer({question: 5.0})
    call, channel = session(dialogue, Recognizer({10: "К терапевту"}), synthesizer)
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: question in channel.said())

    # кашель длиной в слово: распознавание пустое
    await call.feed(phrase(7))
    await until(lambda: channel.said().count(question) == 2)
    assert len(dialogue.turns) == 1
    await call.close()


async def test_red_flag_answered_before_phrase_ends():
    emergency = "Это может быть опасно. Немедленно звоните сто три."

    def answer(turn):
        if "давит в груди" in turn.text:
            return reply(emergency, transfer=SimpleNamespace(route="R-01", priority="P1"))
        return reply("Ожидаю завершения команды.", kind="partial")

    dialogue = Dialogue(answer, urgent=True)
    texts = {n: "у меня сильно давит в груди" for n in range(10, 40)}
    call, channel = session(dialogue, Recognizer(texts), Synthesizer({emergency: 0.3}))
    await answered(call)
    await call.feed(SPEECH * 15)
    await until(lambda: emergency in channel.said())
    assert call.endpointer.in_phrase
    assert dialogue.turns == []

    # пациент договаривает поверх экстренной реплики — её не обрываем и не повторяем
    await call.feed(SPEECH * 10 + QUIET * 12)
    await until(lambda: channel.of("end"))
    assert channel.of("stop") == []
    assert channel.said().count(emergency) == 1
    assert channel.of("end")[0] == {
        "type": "end",
        "reason": "transfer",
        "route": "R-01",
        "priority": "P1",
        "cause": "dialogue",
    }
    await call.close()


async def test_pause_mid_sentence_glues_phrases():
    dialogue = Dialogue(lambda turn: reply("Хорошо."))
    recognizer = Recognizer({10: "Мне к терапевту", 18: "Мне к терапевту на пятницу"}, delay=0.2)
    call, channel = session(dialogue, recognizer)
    await answered(call)
    await call.feed(phrase(10))
    # ответа ещё нет — пациент договаривает
    await call.feed(phrase(8))
    await until(lambda: "Хорошо." in channel.said())
    assert [(t.turn_id, t.text) for t in dialogue.turns] == [(2, "Мне к терапевту на пятницу")]
    await call.close()


async def test_phrase_after_dialogue_answered_is_new_turn():
    answer = "Назовите, пожалуйста, фамилию, имя, отчество."

    class SlowSynthesizer(Synthesizer):
        def synthesize(self, speech):
            if "фамилию" in speech:
                time.sleep(0.3)
            return super().synthesize(speech)

    dialogue = Dialogue(lambda turn: reply(answer))
    recognizer = Recognizer({10: "Мне к терапевту", 8: "Тестов Александр"})
    call, channel = session(dialogue, recognizer, SlowSynthesizer())
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: len(dialogue.turns) == 1)
    # ответ ещё синтезируется, а пациент уже говорит дальше. Ответ просит ФИО, и, если шлюз
    # успел его получить, фраза кончается только после паузы диктовки — тишины даём с запасом
    await call.feed(SPEECH * 8 + QUIET * 25)
    await until(lambda: len(dialogue.turns) == 2)
    assert [(t.turn_id, t.text) for t in dialogue.turns] == [
        (1, "Мне к терапевту"),
        (2, "Тестов Александр"),
    ]
    await call.close()


async def test_red_flag_reaches_dialogue_while_confirmation_is_slow():
    """Итоговое «да» ждёт МИС, а пациент уже говорит о боли в груди: промежуточная реплика
    должна дойти до диалога сразу, чтобы он успел отозвать запись."""
    emergency = "Немедленно звоните сто три."
    red = b"\x20\x00" * (FRAME_BYTES // 2)

    class AnyVoice(Vad):
        def probability(self, frame):
            return 0.9 if any(frame) else 0.05

    class Words(Recognizer):
        def transcribe(self, pcm):
            return "у меня давит в груди" if red in pcm else super().transcribe(pcm)

    class SlowConfirmation(Dialogue):
        def __init__(self):
            super().__init__(None)
            self.log = []

        async def handle(self, call_id, turn):
            self.calls.append(turn)
            self.log.append(("start", turn.turn_id, turn.is_final))
            if turn.is_final:
                await asyncio.sleep(0.6)
                self.log.append(("end", turn.turn_id, turn.is_final))
                return reply("Вы записаны.")
            if "груди" in turn.text:
                return reply(emergency, transfer=SimpleNamespace(route="R-01", priority="P1"))
            return reply("Ожидаю завершения команды.", kind="partial")

    dialogue = SlowConfirmation()
    channel = Channel()
    call = CallSession(
        "call-1",
        channel,
        dialogue,
        AnyVoice(),
        Words({10: "Да, верно"}),
        Synthesizer(),
        today=lambda: TODAY,
        settings=SETTINGS.model_copy(update={"wait_phrase_s": 5}),
    )
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: ("start", 1, True) in dialogue.log)
    await call.feed(red * 20)
    await until(lambda: emergency in channel.said())
    # экстренный ответ прозвучал, пока прежнее «да» ещё ждёт МИС
    assert ("start", 2, False) in dialogue.log
    assert ("end", 1, True) not in dialogue.log
    # ответ на прежнее «да» пришёл позже и уже не звучит
    await until(lambda: ("end", 1, True) in dialogue.log)
    await asyncio.sleep(0.05)
    assert "Вы записаны." not in channel.said()
    await call.close()


async def test_slow_answer_gets_wait_phrase():
    dialogue = Dialogue(lambda turn: reply("Есть завтра в девять."), delay=0.6)
    call, channel = session(dialogue, Recognizer({10: "К терапевту"}))
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: "Есть завтра в девять." in channel.said())
    assert channel.said()[-2:] == ["Одну секунду, проверяю расписание.", "Есть завтра в девять."]
    await call.close()


async def test_dialogue_failure_goes_to_registrar():
    def answer(turn):
        raise RuntimeError("сбой")

    call, channel = session(Dialogue(answer), Recognizer({10: "К терапевту"}))
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: channel.of("end"))
    assert channel.said()[-1] == "Извините, система не отвечает. Соединяю с регистратором."
    assert channel.of("end")[0]["route"] == "R-06"
    await call.close()


async def test_silence_prompts_then_hangs_up():
    call, channel = session(Dialogue(lambda turn: reply("—")), Recognizer({}), silence_prompt_s=0.1)
    await call.start()
    await until(lambda: channel.of("end"))
    assert channel.said()[1:] == ["Вы меня слышите?", "Вас не слышно. Перезвоните, пожалуйста."]
    assert channel.of("end")[0]["reason"] == "silence"
    assert call.finished.is_set()
    await call.close()


async def test_wait_for_mis_holds_silence_prompts_until_retry_time():
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=1.5)
    waiting = "Подождите около 5 секунд и скажите «ещё раз»."
    dialogue = Dialogue(
        lambda turn: reply(
            "Система записи сейчас занята.", waiting, code="WAIT_MIS", retry_at=retry_at
        )
    )
    call, channel = session(dialogue, Recognizer({10: "Мне к терапевту"}), silence_prompt_s=0.4)
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: waiting in channel.said())
    asked = time.monotonic()
    await until(lambda: "Система записи освободилась, можно продолжать." in channel.said())
    # Пока звонящий честно ждёт МИС, «Вы меня слышите?» не звучит и звонок не кончается.
    assert time.monotonic() - asked >= 0.6
    said = channel.said()
    assert said[said.index(waiting) + 1] == "Система записи освободилась, можно продолжать."
    # После паузы — обычная проверка тишины.
    await until(lambda: channel.of("end"))
    assert channel.said()[-2:] == ["Вы меня слышите?", "Вас не слышно. Перезвоните, пожалуйста."]
    await call.close()


async def test_goodbye_ends_call():
    dialogue = Dialogue(lambda turn: reply("Всего доброго, будьте здоровы!", phase="ended"))
    call, channel = session(dialogue, Recognizer({10: "Нет, спасибо"}))
    await answered(call)
    await call.feed(phrase(10))
    await until(lambda: channel.of("end"))
    assert channel.of("end")[0]["reason"] == "ended"
    # после решения закончить разговор новые фразы в диалог не идут
    await call.feed(phrase(10))
    await asyncio.sleep(0.05)
    assert len(dialogue.turns) == 1
    await call.close()
