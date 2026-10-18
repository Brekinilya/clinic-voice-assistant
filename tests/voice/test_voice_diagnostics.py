"""Телефонный канал обычно отбрасывает служебные события — панель должна увидеть их до этого."""

from app.diagnostics.channel import ObservedChannel
from tests.test_diagnostics import recording
from tests.voice.fakes import Dialogue, Recognizer, phrase, reply
from tests.voice.test_session import answered, session, until


async def test_speech_transcript_response_timing_and_close_are_visible():
    dialogue = Dialogue(lambda turn: reply("Назовите фамилию."))
    async with recording() as recorder:
        dialogue.debug = recorder
        call, channel = session(dialogue, Recognizer({10: "К хирургу"}))
        try:
            await answered(call)
            await call.feed(phrase(10))
            await until(lambda: len(channel.of("timing")) == 1)
        finally:
            await call.close()
        await recorder.flush()
        events = await recorder.store.read("call-1")
        kinds = {event["kind"] for event in events}
        assert {
            "voice.ready",
            "voice.transcript",
            "voice.reply",
            "voice.speak",
            "voice.timing",
            "voice.closed",
        } <= kinds
        assert any(e["data"].get("text") == "К хирургу" and e["data"].get("final") for e in events)
        assert channel.audio


async def test_sip_events_are_captured_even_when_phone_channel_ignores_them():
    class PhoneChannel:
        async def send_json(self, message):
            pass

        async def send_bytes(self, data):
            self.audio = data

    async with recording() as recorder:
        phone = PhoneChannel()
        channel = ObservedChannel(phone, recorder, "sip-1")
        await channel.send_json(
            {
                "type": "transcript",
                "turn_id": 2,
                "text": "Да",
                "final": True,
                "secret": "not-for-debug",
            }
        )
        await channel.send_bytes(b"not-a-recording")
        await recorder.flush()
        events = await recorder.store.read("sip-1")
        assert events[0]["data"]["source"] == "SIP"
        assert events[0]["data"]["text"] == "Да"
        assert "secret" not in str(events) and "not-a-recording" not in str(events)
        assert phone.audio == b"not-a-recording"
