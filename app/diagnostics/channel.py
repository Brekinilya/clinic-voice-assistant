"""События одинаковы для браузера и SIP; бинарное аудио никогда не сохраняется."""


class ObservedChannel:
    def __init__(self, channel, recorder, call_id):
        self.channel = channel
        self.recorder = recorder
        self.call_id = call_id
        self.source = "SIP" if type(channel).__name__ == "PhoneChannel" else "browser"

    async def send_json(self, message):
        kind = message.get("type")
        fields = {
            "ready": (),
            "transcript": ("text", "final", "status"),
            "reply": ("utterances", "code", "phase"),
            "speak": ("text", "sample_rate"),
            "stop": (),
            "timing": ("asr_ms", "dialog_ms", "tts_ms", "total_ms", "since_speech_ms"),
            "end": ("reason", "route", "priority", "cause"),
            "question": ("kind",),
        }
        if kind in fields:
            self.recorder.emit(
                self.call_id,
                message.get("turn_id"),
                "voice." + kind,
                source=self.source,
                **{key: message[key] for key in fields[kind] if key in message},
            )
        await self.channel.send_json(message)

    async def send_bytes(self, data):
        await self.channel.send_bytes(data)
