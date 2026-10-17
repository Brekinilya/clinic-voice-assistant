"""После успешной записи отвечаем на новый вопрос, не повторяя POST в МИС."""

import pytest
from fixture_bridge import pipeline, turn
from test_budget_dialogue import reserve

from app.dialogue.models import Phase
from tests.voice.fakes import Recognizer, phrase, reply
from tests.voice.test_session import answered, session, until


@pytest.mark.parametrize("answer", ["Да", "Да, да", "Нет, спасибо", "Не нужно", "Повторите"])
async def test_followup_after_booking_is_local_and_never_books_twice(answer):
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        booked = await turn(client, 4, "Да")
        assert booked["code"] == "BOOKED"
        assert booked["utterances"][-1] == "Могу ещё чем-то помочь?"
        assert booked["expect"] == "yes_no"
        mark = len(emulator.log)
        result = await turn(client, 5, answer)
        assert result["code"] != "NOT_UNDERSTOOD"
        assert result["transfer"] is None
        assert emulator.log[mark:] == [] and emulator.write_count == 1
        if answer.startswith(("Нет", "Не нужно")):
            assert result["phase"] == "ended"
        elif answer.startswith("Да"):
            assert result["code"] == "MORE_HELP_REQUIRED" and result["expect"] == "free"
        else:
            assert result["expect"] == "yes_no"


async def test_audibility_yes_needs_a_fresh_booking_confirmation():
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        call, channel = session(service, Recognizer({9: "Да, я вас слышу", 10: "Да"}))
        call.call_id, call.turn_id = "scenario", 3
        try:
            await answered(call)
            await call._deliver(3, reply("Подтвердите выбранный приём.", phase="held"))
            call.settings = call.settings.model_copy(update={"silence_prompt_s": 0.05})
            call._watch_silence()
            await until(lambda: "Вы меня слышите?" in channel.said())
            await call.feed(phrase(9))
            await until(lambda: call._responder is not None and call._responder.done())
            assert emulator.write_count == 0
            assert (await service.store.inspect("scenario")).phase == Phase.HELD
            call.settings = call.settings.model_copy(update={"silence_prompt_s": 30})
            await until(lambda: not call.speaking())
            await call.feed(phrase(10))
            await until(lambda: any(r["code"] == "BOOKED" for r in channel.of("reply")))
            assert emulator.write_count == 1
            state = await service.store.inspect("scenario")
            assert state.phase == Phase.BOOKED and state.last_turn_id == 5
        finally:
            await call.close()
