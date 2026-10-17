"""T03: голосовое непонимание не подтверждает ранее подготовленную запись в МИС."""

import asyncio

from fixture_bridge import pipeline, turn

from app.voice.session import CallSession
from tests.voice.fakes import Channel, Recognizer, Synthesizer, Vad, phrase
from tests.voice.test_session import SETTINGS, until


async def test_yes_to_operator_after_empty_asr_does_not_commit_a_held_booking():
    async with pipeline() as (client, service, emulator, clock, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, 12.03.1978")
        held = await turn(client, 3, "Первый")
        assert held["phase"] == "held"
        channel = Channel()
        call = CallSession(
            "scenario",
            channel,
            service,
            Vad(),
            Recognizer({9: "Да"}),
            Synthesizer(),
            today=lambda: clock.now().date(),
            settings=SETTINGS.model_copy(update={"partial_ms": 5000}),
        )
        call.turn_id = 3
        try:
            await call.start()
            await until(lambda: not call.speaking())
            for count in (1, 2):
                await call.feed(phrase(10))
                await until(lambda: len(channel.of("transcript")) >= count)
                await until(lambda: not call.speaking())
            assert call._operator_offered and emulator.write_count == 0
            await call.feed(phrase(9))
            await until(lambda: channel.of("end"))
            assert channel.of("end")[-1]["route"] == "R-05"
            assert emulator.write_count == emulator.patch_count == 0
            assert (await service.store.inspect("scenario")).phase == "ended"
        finally:
            await call.close()


async def test_hangup_with_late_asr_yes_cannot_create_a_visit():
    async with pipeline() as (client, service, emulator, clock, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна, 12.03.1978")
        assert (await turn(client, 3, "Первый"))["phase"] == "held"
        channel = Channel()
        call = CallSession(
            "scenario",
            channel,
            service,
            Vad(),
            Recognizer({10: "Да"}, delay=0.1),
            Synthesizer(),
            today=lambda: clock.now().date(),
            settings=SETTINGS.model_copy(update={"partial_ms": 5000}),
        )
        call.turn_id = 3
        await call.start()
        await until(lambda: not call.speaking())
        await call.feed(phrase(10))
        await call.close()
        await asyncio.sleep(0.15)
        assert emulator.write_count == emulator.patch_count == 0
        assert (await service.store.inspect("scenario")).phase == "ended"
