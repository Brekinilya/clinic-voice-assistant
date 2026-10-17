"""Ответ о слышимости не подтверждает старую бронь и не запускает цикл непонимания."""

import asyncio
from types import SimpleNamespace

import pytest

from tests.voice.fakes import QUIET, SPEECH, Dialogue, Recognizer, phrase, reply
from tests.voice.test_session import answered, session, until
from tests.voice.test_session_failures import offer_operator_after_empty_recognition


async def ask_audibility(call, channel):
    call.settings = call.settings.model_copy(update={"silence_prompt_s": 0.05})
    call._watch_silence()
    await until(lambda: "Вы меня слышите?" in channel.said())
    # После первого вопроса дальше проверяем ответы, а не гоняем таймер 50 мс.
    call.settings = call.settings.model_copy(update={"silence_prompt_s": 30})


@pytest.mark.parametrize(
    "text",
    ["Да", "Да, да", "Да, я вас слышу", "Слышу", "Нет", "Я вас не слышу", "Да, я вас не слышу"],
)
async def test_audibility_answer_repeats_question_without_final_backend_turn(text):
    question = "Подтвердите запись. Всё верно?"
    dialogue = Dialogue(lambda turn: reply("Запись создана", code="BOOKED"))
    call, channel = session(dialogue, Recognizer({9: text}), partial_ms=5000)
    try:
        await answered(call)
        await call._deliver(0, reply(question, phase="held"))
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await until(lambda: call._responder is not None and call._responder.done())
        assert not dialogue.turns and not channel.of("end")
        assert channel.said().count(question) == 2
        assert "Запись создана" not in channel.said()
        assert dialogue.calls and not dialogue.calls[-1].is_final
    finally:
        await call.close()


async def test_new_request_after_audibility_prompt_reaches_backend():
    dialogue = Dialogue(lambda turn: reply("Уточняю отмену"))
    call, channel = session(dialogue, Recognizer({9: "Отмените мою запись"}), partial_ms=5000)
    try:
        await answered(call)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await until(lambda: dialogue.turns)
        assert dialogue.turns[-1].text == "Отмените мою запись"
    finally:
        await call.close()


async def test_yes_to_audibility_does_not_accept_the_previous_operator_offer():
    dialogue = Dialogue(lambda turn: reply("Запись создана"))
    call, channel = session(dialogue, Recognizer({9: "Да, я вас слышу", 8: "Не нужно"}))
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await until(lambda: call._responder is not None and call._responder.done())
        assert call._operator_offered and not call.closing and not dialogue.turns
        await until(lambda: not call.speaking())
        await call.feed(phrase(8))
        await until(lambda: call._responder is not None and call._responder.done())
        assert not call._operator_offered and not call.closing and not dialogue.turns
    finally:
        await call.close()


async def test_emergency_preempts_audibility_question():
    emergency = reply("Звоните сто три.", transfer=SimpleNamespace(route="R-01", priority="P1"))
    dialogue = Dialogue(lambda turn: emergency, urgent=True)
    call, channel = session(dialogue, Recognizer({9: "Да, давит в груди"}), partial_ms=5000)
    try:
        await answered(call)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["route"] == "R-01"
        assert not dialogue.turns
    finally:
        await call.close()


@pytest.mark.parametrize("text", ["Не нужно", "Нет, спасибо"])
async def test_declining_operator_does_not_reach_old_booking(text):
    dialogue = Dialogue(lambda turn: reply("Запись создана"))
    call, channel = session(dialogue, Recognizer({9: text}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await call.feed(phrase(9))
        await until(lambda: call._responder is not None and call._responder.done())
        assert not dialogue.turns and not call._operator_offered
        assert not call.closing
    finally:
        await call.close()


@pytest.mark.parametrize("emergency", [False, True])
async def test_partial_safety_finishes_before_final_authorization(emergency):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowPartial(Dialogue):
        async def handle(self, call_id, turn):
            if not turn.is_final:
                entered.set()
                await release.wait()
            return await super().handle(call_id, turn)

    def answer(turn):
        if not turn.is_final:
            return reply("Звоните сто три.", transfer=SimpleNamespace(route="R-01", priority="P1"))
        return reply("Запись создана", code="BOOKED")

    dialogue = SlowPartial(answer, urgent=emergency)
    call, channel = session(dialogue, Recognizer({10: "Да"}), partial_ms=320)
    try:
        await answered(call)
        await call.feed(SPEECH * 10)
        await asyncio.wait_for(entered.wait(), 1)
        await call.feed(QUIET * 12)
        await until(lambda: any(t["final"] for t in channel.of("transcript")))
        await asyncio.sleep(0.03)
        assert not dialogue.turns, "Итоговое согласие обогнало проверку промежуточной реплики"
        release.set()
        if emergency:
            await until(lambda: channel.of("end"))
            assert channel.of("end")[-1]["route"] == "R-01"
            assert not dialogue.turns
        else:
            await until(lambda: any(r["code"] == "BOOKED" for r in channel.of("reply")))
            assert len(dialogue.turns) == 1
    finally:
        release.set()
        await call.close()
