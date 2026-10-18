"""Телефония на подставном Asterisk: какие команды ARI шлюз отдаёт на события звонка."""

import asyncio
import socket

import pytest

from app.voice import telephony as telephony_module
from app.voice.rtp import CODECS, RtpSender, Upsampler
from app.voice.telephony import PhoneChannel, Telephony, TelephonySettings

PCM = bytes(range(256)) * 5


class Ari:
    def __init__(self):
        self.calls: list[tuple] = []
        self.operator_fails = False

    async def answer(self, channel):
        self.calls.append(("answer", channel))

    async def hangup(self, channel):
        self.calls.append(("hangup", channel))

    async def bridge(self):
        self.calls.append(("bridge",))
        return "bridge-1"

    async def add(self, bridge, *channels):
        self.calls.append(("add", bridge, *channels))

    async def remove(self, bridge, channel):
        self.calls.append(("remove", bridge, channel))

    async def destroy(self, bridge):
        self.calls.append(("destroy", bridge))

    async def music(self, bridge, on):
        self.calls.append(("music", bridge, on))

    async def external_media(self, host):
        self.calls.append(("external_media", host))
        return "media-1", 10002

    async def originate(self, endpoint, args, timeout):
        self.calls.append(("originate", endpoint, tuple(args), timeout))
        return "operator-1"


class Session:
    def __init__(self):
        self.audio = bytearray()
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def feed(self, pcm):
        self.audio.extend(pcm)

    async def close(self):
        self.closed = True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def line():
    port = free_port()
    settings = TelephonySettings(
        _env_file=None,
        media_host="gateway.test",
        media_bind="127.0.0.1",
        media_ports=(port, port),
        asterisk_rtp_host="127.0.0.1",
        operator_endpoint="PJSIP/operator",
    )
    sessions: list[Session] = []
    said: list[str] = []

    def new_session(call_id, channel):
        sessions.append(Session())
        return sessions[-1]

    async def say(template):
        said.append(template)
        return PCM, 8000

    telephony = Telephony(Ari(), settings, new_session, say, lambda pcm, src, dst: pcm)
    telephony.sessions_made = sessions
    telephony.said = said
    yield telephony
    await telephony.close()


async def settled(condition, timeout: float = 2.0):
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


def start(channel, *args):
    return {"type": "StasisStart", "channel": {"id": channel}, "args": list(args)}


def end(channel):
    return {"type": "StasisEnd", "channel": {"id": channel}}


async def test_incoming_call_bridges_caller_with_gateway(line):
    await line.handle(start("caller-1", "inbound"))
    port = line.settings.media_ports[0]
    assert line.ari.calls == [
        ("answer", "caller-1"),
        ("external_media", f"gateway.test:{port}"),
        ("bridge",),
        ("add", "bridge-1", "caller-1", "media-1"),
    ]
    call = line.calls["caller-1"]
    assert call.rtp.remote == ("127.0.0.1", 10002)
    assert line.sessions_made[0].started

    # своё начало внешнего медиаканала — не новый звонок
    await line.handle(start("media-1"))
    assert len(line.calls) == 1


async def test_caller_audio_reaches_gateway(line):
    await line.handle(start("caller-1", "inbound"))
    port = line.settings.media_ports[0]
    ulaw = CODECS["ulaw"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(RtpSender(ulaw).packet(PCM), ("127.0.0.1", port))
    # ulaw 8 кГц приходит к шлюзу уже PCM 16 кГц
    expected = Upsampler()(ulaw.decode(ulaw.encode(PCM)))
    for _ in range(50):
        if expected in bytes(line.sessions_made[0].audio):
            break
        await asyncio.sleep(0.01)
    assert expected in bytes(line.sessions_made[0].audio)


async def test_silence_fills_gaps_without_rtp(line):
    await line.handle(start("caller-1", "inbound"))
    await asyncio.sleep(0.35)
    # пациент молчит, RTP нет — шлюз всё равно получает тишину и может закончить фразу
    assert len(line.sessions_made[0].audio) >= 3200 * 2
    assert not any(line.sessions_made[0].audio)


async def test_caller_hangup_releases_everything(line):
    await line.handle(start("caller-1", "inbound"))
    line.ari.calls.clear()
    await line.handle(end("caller-1"))
    assert line.ari.calls == [("hangup", "media-1"), ("destroy", "bridge-1")]
    assert line.sessions_made[0].closed
    assert line.calls == {}
    # порт вернулся: следующий звонок его получит
    await line.handle(start("caller-2", "inbound"))
    assert "caller-2" in line.calls


async def test_goodbye_hangs_up_after_reply_played(line):
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    channel = PhoneChannel(line, call)
    await channel.send_json({"type": "speak", "sample_rate": 16000})
    await channel.send_bytes(PCM * 3)
    await channel.send_json({"type": "end", "reason": "ended"})
    # на 8 кГц этот звук играет 240 мс
    await asyncio.sleep(0.5)
    assert ("hangup", "caller-1") in line.ari.calls


async def test_patient_interrupts_reply(line):
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    channel = PhoneChannel(line, call)
    await channel.send_bytes(PCM * 100)
    await channel.send_json({"type": "stop", "turn_id": 1})
    await asyncio.wait_for(call.rtp.drained(), 0.1)


async def test_transfer_connects_registrar(line):
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    line.ari.calls.clear()
    await line.finish(call, {"reason": "transfer", "route": "R-01", "priority": "P1"})
    assert line.ari.calls == [
        ("music", "bridge-1", True),
        ("originate", "PJSIP/operator", ("operator", "caller-1"), 20),
    ]
    line.ari.calls.clear()
    await line.handle(start("operator-1", "operator", "caller-1"))
    assert line.ari.calls == [
        ("music", "bridge-1", False),
        ("add", "bridge-1", "operator-1"),
        ("remove", "bridge-1", "media-1"),
        ("hangup", "media-1"),
    ]
    # регистратор положил трубку — разговор окончен
    await line.handle(end("operator-1"))
    assert ("hangup", "caller-1") in line.ari.calls


@pytest.mark.parametrize(
    "priority, template, timeout",
    [("P1", "emergency.no_answer", 20), ("P3", "transfer.no_answer", 90)],
)
async def test_registrar_does_not_answer(line, priority, template, timeout):
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    await line.finish(call, {"reason": "transfer", "priority": priority})
    assert line.ari.calls[-1][-1] == timeout
    await line.handle({"type": "ChannelDestroyed", "channel": {"id": "operator-1"}})
    await settled(lambda: line.ari.calls[-1] == ("hangup", "caller-1"))
    assert line.said == [template]


async def test_journal_timeout_still_originates_once_and_duplicate_finish_is_ignored(
    line, monkeypatch
):
    monkeypatch.setattr(telephony_module, "HANDOFF_JOURNAL_TIMEOUT_S", 0.02)
    entered = asyncio.Event()

    async def blocked_start(_):
        entered.set()
        await asyncio.Event().wait()

    line.on_transfer_start = blocked_start
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    first = asyncio.create_task(
        line.finish(call, {"reason": "transfer", "route": "R-01", "priority": "P1"})
    )
    await entered.wait()
    await line.finish(call, {"reason": "transfer", "route": "R-01", "priority": "P1"})
    await first
    assert sum(c[0] == "originate" for c in line.ari.calls) == 1


async def test_patient_hangup_during_journal_write_prevents_originate(line, monkeypatch):
    monkeypatch.setattr(telephony_module, "HANDOFF_JOURNAL_TIMEOUT_S", 0.2)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_start(_):
        entered.set()
        await release.wait()

    line.on_transfer_start = blocked_start
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    transfer = asyncio.create_task(
        line.finish(call, {"reason": "transfer", "route": "R-01", "priority": "P1"})
    )
    await entered.wait()
    await line.handle(end("caller-1"))
    release.set()
    await transfer
    assert not any(c[0] == "originate" for c in line.ari.calls)
