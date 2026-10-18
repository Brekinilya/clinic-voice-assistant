"""V01: голосовые тупики. Каждый исход заканчивается репликой, таймером, переводом или концом."""

import asyncio
import time

import pytest

from app.voice.audibility import split_audibility
from app.voice.telephony import Telephony, TelephonySettings
from tests.voice.fakes import Dialogue, Recognizer, Synthesizer, phrase, reply
from tests.voice.test_audibility_context import ask_audibility
from tests.voice.test_session import answered, session, until
from tests.voice.test_session_failures import offer_operator_after_empty_recognition
from tests.voice.test_telephony import PCM, Ari, Session, free_port, settled, start

OFFER = "Похоже, я вас плохо понимаю. Соединить с сотрудником?"
CONFIRM = "Записать вас к хирургу на 20 октября в 10:00? Подтвердите, пожалуйста."


async def processed(call):
    """Реплика обработана и отзвучала: следующая фраза — новая, а не продолжение прежней."""
    await until(lambda: call._responder is not None and call._responder.done())
    await until(lambda: not call.speaking())


# 1. R-05 без цикла предложений


async def test_declined_operator_then_empty_asr_transfers_once():
    dialogue = Dialogue(lambda turn: reply("Не должен прозвучать"))
    call, channel = session(dialogue, Recognizer({9: "Нет"}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await call.feed(phrase(9))
        await processed(call)
        assert not call.closing and not call._operator_offered
        await call.feed(phrase(10))
        await until(lambda: channel.of("end"))
        assert [e["route"] for e in channel.of("end")] == ["R-05"]
        assert channel.of("end")[0]["cause"] == "not_understood"
        assert channel.said().count(OFFER) == 1
        assert len(channel.of("question")) == 1
        assert not dialogue.turns
    finally:
        await call.close()


async def test_declined_operator_then_not_understood_text_transfers_once():
    dialogue = Dialogue(lambda turn: reply("Не поняла.", code="NOT_UNDERSTOOD"))
    call, channel = session(dialogue, Recognizer({9: "Нет", 7: "кхм ну это"}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await call.feed(phrase(9))
        await processed(call)
        await call.feed(phrase(7))
        await until(lambda: channel.of("end"))
        assert [e["route"] for e in channel.of("end")] == ["R-05"]
        assert channel.said().count(OFFER) == 1
        assert len(dialogue.turns) == 1
    finally:
        await call.close()


async def test_declined_operator_repeats_the_question_before_the_offer():
    dialogue = Dialogue(lambda turn: reply(CONFIRM, phase="held"))
    call, channel = session(dialogue, Recognizer({8: "Запишите к хирургу", 9: "Нет"}))
    try:
        await answered(call)
        await call.feed(phrase(8))
        await processed(call)
        for _ in range(2):
            await call.feed(phrase(10))
            await processed(call)
        assert channel.said()[-1] == OFFER
        await call.feed(phrase(9))
        await processed(call)
        assert channel.said()[-1] == CONFIRM
        assert len(dialogue.turns) == 1
    finally:
        await call.close()


# 2. Смешанные пустой ASR и непонятый текст: один общий счёт


async def test_empty_then_not_understood_then_empty_counts_once_each():
    dialogue = Dialogue(lambda turn: reply("Не поняла.", code="NOT_UNDERSTOOD"))
    call, channel = session(dialogue, Recognizer({7: "кхм ну это"}), partial_ms=5000)
    try:
        await answered(call)
        await call.feed(phrase(10))
        await processed(call)
        assert call._misunderstandings == 1
        await call.feed(phrase(7))
        await processed(call)
        # Второе непонимание подряд: вместо ответа диалога — предложение сотрудника
        assert call._misunderstandings == 2 and channel.said()[-1] == OFFER
        await call.feed(phrase(10))
        await until(lambda: channel.of("end"))
        assert [e["route"] for e in channel.of("end")] == ["R-05"]
        assert len(dialogue.turns) == 1
    finally:
        await call.close()


# 3. Слышимость не подтверждает запись


async def test_hearing_check_repeats_confirmation_and_only_next_yes_reaches_dialogue():
    def answer(turn):
        return reply("Вы записаны.", phase="booked") if turn.text == "Да" else reply(CONFIRM)

    dialogue = Dialogue(answer)
    texts = {8: "Запишите к хирургу", 9: "Да, я вас слышу", 7: "Да"}
    call, channel = session(dialogue, Recognizer(texts))
    try:
        await answered(call)
        await call.feed(phrase(8))
        await processed(call)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await processed(call)
        assert channel.said()[-1] == CONFIRM
        # Ответ на проверку звука в диалог как итоговая реплика не ушёл
        assert [t.text for t in dialogue.turns] == ["Запишите к хирургу"]
        await call.feed(phrase(7))
        await processed(call)
        assert [t.text for t in dialogue.turns] == ["Запишите к хирургу", "Да"]
        assert channel.said().count("Вы записаны.") == 1
    finally:
        await call.close()


async def test_command_after_hearing_answer_reaches_dialogue_without_the_yes():
    dialogue = Dialogue(lambda turn: reply("Какую запись отменить?"))
    call, channel = session(dialogue, Recognizer({9: "Да, слышу, но отмените запись"}))
    try:
        await answered(call)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await until(lambda: dialogue.turns)
        assert dialogue.turns[-1].text == "отмените запись"
    finally:
        await call.close()


async def test_hearing_answer_does_not_reset_the_path_to_operator():
    dialogue = Dialogue(lambda turn: reply("Не должен прозвучать"))
    call, channel = session(dialogue, Recognizer({9: "Да, слышу"}), partial_ms=5000)
    try:
        await offer_operator_after_empty_recognition(call, channel)
        await ask_audibility(call, channel)
        await call.feed(phrase(9))
        await processed(call)
        # Повторили действующий вопрос — предложение сотрудника, а не старый вопрос
        assert channel.said()[-1] == OFFER and call._misunderstandings == 2
        await call.feed(phrase(10))
        await until(lambda: channel.of("end"))
        assert channel.of("end")[-1]["route"] == "R-05"
    finally:
        await call.close()


@pytest.mark.parametrize(
    "text, answer, rest",
    [
        ("Да", "yes", ""),
        ("Да, я вас слышу", "yes", ""),
        ("Да, слышу, но отмените запись", "yes", "отмените запись"),
        ("Слышу. Запишите к лору", "yes", "Запишите к лору"),
        ("Плохо слышно", "no", ""),
        ("Нет, не слышу вас, перезвоните", "no", "перезвоните"),
        ("Отмените запись", None, "Отмените запись"),
    ],
)
def test_hearing_answer_is_split_from_the_command(text, answer, rest):
    assert split_audibility(text) == (answer, rest)


# 4. Сбой ASR — не непонимание; таймаут диалога — отдельная причина


async def test_asr_failure_counter_resets_after_recognized_speech():
    class FlakyRecognizer(Recognizer):
        def transcribe(self, pcm):
            text = super().transcribe(pcm)
            if text == "сбой":
                raise RuntimeError("ошибка движка")
            return text

    dialogue = Dialogue(lambda turn: reply("Слушаю."))
    call, channel = session(dialogue, FlakyRecognizer({8: "сбой", 7: "Запишите"}), partial_ms=5000)
    try:
        await answered(call)
        for frames in (8, 7, 8):
            await call.feed(phrase(frames))
            await processed(call)
        assert not call.closing and not channel.of("end")
        assert len(dialogue.turns) == 1
    finally:
        await call.close()


async def test_dialogue_timeout_is_a_technical_exit_not_a_misunderstanding():
    dialogue = Dialogue(lambda turn: reply("Поздний ответ"), delay=0.5)
    call, channel = session(
        dialogue, Recognizer({7: "Запишите"}), partial_ms=5000, dialogue_timeout_s=0.2
    )
    try:
        await answered(call)
        await call.feed(phrase(7))
        await until(lambda: channel.of("end"), timeout=2)
        assert channel.of("end")[-1]["route"] == "R-06"
        assert channel.of("end")[-1]["cause"] == "dialogue_timeout"
        assert call._misunderstandings == 0
        await asyncio.sleep(0.4)
        assert "Поздний ответ" not in channel.said()
    finally:
        await call.close()


# 5. Новая речь во время медленного подтверждения


async def test_new_speech_during_slow_confirmation_revokes_and_late_success_is_silent():
    class SlowConfirmation(Dialogue):
        async def handle(self, call_id, turn):
            self.calls.append(turn)
            if not turn.is_final:
                return reply("", kind="partial")
            if turn.text == "Да":
                await asyncio.sleep(0.4)
                return reply("Вы записаны.", phase="booked")
            return reply("Хорошо, не записываю.")

    dialogue = SlowConfirmation(None)
    call, channel = session(dialogue, Recognizer({7: "Да", 9: "Нет, не надо"}), partial_ms=5000)
    try:
        await answered(call)
        await call.feed(phrase(7))
        await until(lambda: dialogue.turns)
        await call.feed(phrase(9))
        # Отзыв прежнего «да» пришёл до того, как диалог закончил прошлую реплику
        assert ("call-1", 2) in dialogue.started
        await until(lambda: "Хорошо, не записываю." in channel.said(), timeout=2)
        assert "Вы записаны." not in channel.said()
        # Прежнее «да» ушло в диалог один раз: повторной отправки нет
        assert [t.text for t in dialogue.turns] == ["Да", "Нет, не надо"]
    finally:
        await call.close()


# 6. Завершение: PROCESSING_INTERRUPTED и отбой на каждом этапе


async def test_interrupted_processing_closes_the_call_through_registrar():
    interrupted = reply(
        "Обработка прервана. Завершите разговор.",
        code="PROCESSING_INTERRUPTED",
        phase="interrupted",
    )
    dialogue = Dialogue(lambda turn: interrupted)
    call, channel = session(dialogue, Recognizer({7: "Запишите"}), partial_ms=5000)
    try:
        await answered(call)
        await call.feed(phrase(7))
        await until(lambda: channel.of("end"))
        end = channel.of("end")[-1]
        assert (end["route"], end["cause"]) == ("R-06", "interrupted")
        assert "Обработка прервана. Завершите разговор." not in channel.said()
        await call.feed(phrase(7))
        await asyncio.sleep(0.1)
        assert len(dialogue.turns) == 1
    finally:
        await call.close()


class CountingDialogue(Dialogue):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ends = 0

    async def end(self, call_id):
        self.ends += 1


async def test_hangup_while_dialogue_works_discards_late_reply_and_closes_once():
    dialogue = CountingDialogue(lambda turn: reply("Поздний ответ"), delay=0.3)
    call, channel = session(dialogue, Recognizer({7: "Запишите"}), partial_ms=5000)
    await answered(call)
    await call.feed(phrase(7))
    await until(lambda: dialogue.turns)
    await call.close()
    await call.close()
    await asyncio.sleep(0.4)
    assert "Поздний ответ" not in channel.said()
    assert not channel.of("end") and dialogue.ends == 1


async def test_hangup_during_synthesis_sends_no_audio_afterwards():
    class SlowSynthesizer(Synthesizer):
        def synthesize(self, speech):
            if speech.startswith("Долгий"):
                time.sleep(0.2)
            return super().synthesize(speech)

    synthesizer = SlowSynthesizer()
    dialogue = Dialogue(lambda turn: reply("Долгий ответ"))
    call, channel = session(dialogue, Recognizer({7: "Запишите"}), synthesizer, partial_ms=5000)
    await answered(call)
    await call.feed(phrase(7))
    await until(lambda: any(s.startswith("Долгий") for s in synthesizer.spoken))
    audio = len(channel.audio)
    await call.close()
    await asyncio.sleep(0.3)
    assert "Долгий ответ" not in channel.said() and len(channel.audio) == audio


# 7. Исход перевода: одна попытка — один результат


@pytest.fixture
async def phone():
    port = free_port()
    settings = TelephonySettings(
        _env_file=None,
        media_host="gateway.test",
        media_bind="127.0.0.1",
        media_ports=(port, port),
        operator_endpoint="PJSIP/operator",
    )
    results = []
    said = []

    async def say(template):
        said.append(template)
        return PCM, 8000

    telephony = Telephony(
        Ari(),
        settings,
        lambda call_id, channel: Session(),
        say,
        lambda pcm, s, d: pcm,
        on_transfer=results.append,
    )
    telephony.results = results
    telephony.said = said
    yield telephony
    await telephony.close()


def destroyed(channel, cause=None):
    return {"type": "ChannelDestroyed", "channel": {"id": channel}, "cause": cause}


async def transfer(phone, route="R-05", priority="P3"):
    await phone.handle(start("caller-1", "inbound"))
    call = phone.calls["caller-1"]
    await phone.finish(call, {"reason": "transfer", "route": route, "priority": priority})
    return call


async def test_no_answer_is_reported_once_even_when_the_event_repeats(phone):
    await transfer(phone)
    for _ in range(2):
        await phone.handle(destroyed("operator-1", 19))
    await settled(lambda: phone.ari.calls[-1] == ("hangup", "caller-1"))
    [result] = phone.results
    assert (result.outcome, result.reason, result.route) == ("no_answer", "q850_19", "R-05")
    assert result.call_id == "caller-1" and result.attempt_id
    assert phone.said == ["transfer.no_answer"]


async def test_rejected_originate_is_a_failed_transfer(phone):
    async def rejected(endpoint, args, timeout):
        from app.voice.telephony import AriError

        raise AriError("POST channels: 400")

    phone.ari.originate = rejected
    await transfer(phone)
    [result] = phone.results
    assert (result.outcome, result.reason) == ("failed", "originate_rejected")
    assert phone.ari.calls[-1] == ("hangup", "caller-1")


async def test_connected_transfer_is_reported_after_the_bridge_not_on_request(phone):
    call = await transfer(phone, "R-01", "P1")
    # Запрос маршрутизации ещё не соединение
    assert phone.results == [] and not call.connected
    await phone.handle(start("operator-1", "operator", "caller-1"))
    await phone.handle({"type": "StasisEnd", "channel": {"id": "operator-1"}})
    await phone.handle(destroyed("operator-1", 16))
    assert [r.outcome for r in phone.results] == ["connected"]
    assert ("hangup", "caller-1") in phone.ari.calls


async def test_caller_hangup_while_ringing_aborts_and_late_answer_is_dropped(phone):
    await transfer(phone)
    await phone.handle({"type": "StasisEnd", "channel": {"id": "caller-1"}})
    phone.ari.calls.clear()
    await phone.handle(start("operator-1", "operator", "caller-1"))
    await phone.handle(destroyed("operator-1", 16))
    assert [(r.outcome, r.reason) for r in phone.results] == [("aborted", "caller_hung_up")]
    assert not any(c[0] == "add" for c in phone.ari.calls)
    assert phone.said == []


async def test_repeated_end_event_starts_one_transfer(phone):
    call = await transfer(phone)
    await phone.finish(call, {"reason": "transfer", "route": "R-05", "priority": "P3"})
    assert sum(c[0] == "originate" for c in phone.ari.calls) == 1


async def test_failing_result_receiver_does_not_break_the_call(phone):
    def broken(result):
        raise RuntimeError("получатель недоступен")

    phone.on_transfer = broken
    await transfer(phone)
    await phone.handle(destroyed("operator-1", 19))
    await settled(lambda: phone.ari.calls[-1] == ("hangup", "caller-1"))
