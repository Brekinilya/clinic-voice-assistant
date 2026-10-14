"""Звонок через шлюз на подставных движках: без моделей, звук — кадры тишины и «речи»."""

import asyncio
import time
from datetime import date
from types import SimpleNamespace

import pytest

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
    # ответ ещё синтезируется, а пациент уже говорит дальше
    await call.feed(phrase(8))
    await until(lambda: len(dialogue.turns) == 2)
    assert [(t.turn_id, t.text) for t in dialogue.turns] == [
        (1, "Мне к терапевту"),
        (2, "Тестов Александр"),
    ]
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
