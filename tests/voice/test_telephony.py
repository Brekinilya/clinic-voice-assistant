"""Телефония на подставном Asterisk: какие команды ARI шлюз отдаёт на события звонка."""

import asyncio
import socket
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.clock import CLINIC_TIMEZONE
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import DialogueState
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.service import HandoffService, HandoffSettings
from app.mis.ports import MisError
from app.voice import telephony as telephony_module
from app.voice.rtp import CODECS, RtpSender, Upsampler
from app.voice.session import CallSession
from app.voice.settings import VoiceSettings
from app.voice.telephony import AriError, PhoneChannel, Telephony, TelephonySettings
from tests.voice.fakes import Synthesizer, Vad, phrase

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


class FixedClock:
    def now(self):
        return datetime(2026, 10, 19, 10, tzinfo=CLINIC_TIMEZONE)


class NoMis:
    async def create_escalation(self, payload):
        raise AssertionError("MIS must remain outside this fake-ARI integration")


class SequenceRecognizer:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.heard = []

    def transcribe(self, pcm):
        self.heard.append(len(pcm))
        return next(self.texts, "")


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


async def test_verified_call_transfer_delivers_mis_escalation(line):
    clock = FixedClock()
    dialogue_store = MemoryDialogueStore()
    call_id = "verified-t08-call"
    dialogue_store.states[call_id] = DialogueState(call_id=call_id, client_id=71)
    handoff_store = MemoryHandoffStore()

    class MisRecorder:
        def __init__(self):
            self.posts = []

        async def create_escalation(self, payload):
            self.posts.append(payload)
            return SimpleNamespace(id=42)

    mis = MisRecorder()
    handoffs = HandoffService(
        handoff_store,
        mis,
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True),
    )

    async def release_call_holds(_):
        return None

    dialogue = DialogueService(
        SimpleNamespace(
            clock=clock,
            clear_call_cache=lambda _: None,
            store=SimpleNamespace(release_call_holds=release_call_holds),
        ),
        dialogue_store,
        b"x" * 32,
        safety=RulesSafety(clock),
        handoffs=handoffs,
    )
    recognizer = SequenceRecognizer(("соедините с оператором",))

    def new_session(call_id, channel):
        return CallSession(
            call_id,
            channel,
            dialogue,
            Vad(),
            recognizer,
            Synthesizer(),
            today=lambda: date(2026, 10, 19),
            settings=VoiceSettings(
                _env_file=None,
                min_speech_ms=192,
                end_silence_ms=320,
                dictation_silence_ms=640,
                partial_ms=5000,
                silence_prompt_s=30,
                goodbye_hangup_s=0.05,
                wait_phrase_s=0.3,
            ),
        )

    line.sessions = new_session

    async def transfer_started(event):
        state = await dialogue_store.inspect(event.call_id)
        await handoffs.transfer_started(event, state)

    line.on_transfer_start = transfer_started
    line.on_transfer = handoffs.transfer_result
    await line.handle(start(call_id, "inbound"))
    session = line.calls[call_id].session
    await settled(lambda: not session.speaking())
    await session.feed(phrase(10))
    await settled(lambda: bool(recognizer.heard))
    await settled(lambda: "operator-1" in line._operators)
    await line.handle(start("operator-1", "operator", call_id))
    await settled(lambda: line.calls[call_id].connected)

    request = await handoff_store.request_for_call(call_id)
    assert request is not None
    assert (request.route, request.priority, request.identity_status, request.client_id) == (
        "R-02",
        "P2",
        "verified",
        71,
    )
    assert request.transfer_state.value == "connected"
    assert await handoffs.deliver_mis_once() == 1
    operation = await handoff_store.get_mis_operation(request.mis_operation_id)
    assert operation is not None and operation.state.value == "applied"
    assert len(mis.posts) == 1
    assert mis.posts[0].client_id == 71
    assert operation.marker in mis.posts[0].description

    await line.handle(end(call_id))


async def test_answered_operator_with_rejected_bridge_is_reported_failed(line, monkeypatch):
    outcomes = []
    original_add = line.ari.add
    clock = FixedClock()
    handoff_store = MemoryHandoffStore()
    handoffs = HandoffService(
        handoff_store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )

    async def reject_operator(bridge, *channels):
        if channels == ("operator-1",):
            raise AriError("bridge rejected")
        await original_add(bridge, *channels)

    monkeypatch.setattr(line.ari, "add", reject_operator)
    line.on_transfer_start = handoffs.transfer_started

    async def record_result(result):
        outcomes.append(result.outcome)
        await handoffs.transfer_result(result)

    line.on_transfer = record_result
    await line.handle(start("caller-rejected-bridge", "inbound"))
    call = line.calls["caller-rejected-bridge"]
    await line.transfer(call, "P2", "R-08")

    await line.handle(start("operator-1", "operator", "caller-rejected-bridge"))
    await settled(lambda: outcomes)

    assert outcomes == ["failed"]
    assert call.connected is False
    assert ("hangup", "operator-1") in line.ari.calls
    request = await handoff_store.request_for_call("caller-rejected-bridge")
    assert request is not None and request.transfer_state.value == "failed"
    assert handoff_store.mis_operations == {}


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


@pytest.mark.parametrize("operator_answers", [False, True])
async def test_p3_operator_wait_repeats_status_without_ending_caller(
    line, monkeypatch, operator_answers
):
    monkeypatch.setattr(telephony_module, "OPERATOR_WAIT_PROMPT_INTERVAL_S", 0.01, raising=False)
    await line.handle(start("caller-p3", "inbound"))
    call = line.calls["caller-p3"]

    await line.transfer(call, "P3", "R-08")

    await settled(lambda: line.said.count("transfer.waiting") >= 2)
    assert ("originate", "PJSIP/operator", ("operator", "caller-p3"), 90) in line.ari.calls
    assert ("hangup", "caller-p3") not in line.ari.calls

    if operator_answers:
        await line.handle(start("operator-1", "operator", "caller-p3"))
        assert call.connected
        prompts = line.said.count("transfer.waiting")
        await asyncio.sleep(0.03)
        assert line.said.count("transfer.waiting") == prompts
    else:
        await line.handle({"type": "ChannelDestroyed", "channel": {"id": "operator-1"}})
        await settled(lambda: ("hangup", "caller-p3") in line.ari.calls)
    assert line.said.count("transfer.waiting") >= 2


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


async def test_journal_timeout_does_not_replace_actual_connected_outcome(line, monkeypatch):
    """Ошибка локального журнала не означает, что реальный перевод не состоялся."""
    monkeypatch.setattr(telephony_module, "HANDOFF_JOURNAL_TIMEOUT_S", 0.02)
    outcomes = []

    async def blocked_start(_):
        await asyncio.Event().wait()

    async def record(result):
        outcomes.append(result.outcome)

    line.on_transfer_start = blocked_start
    line.on_transfer = record
    await line.handle(start("caller-1", "inbound"))
    call = line.calls["caller-1"]
    await line.finish(call, {"reason": "transfer", "route": "R-01", "priority": "P1"})
    await line.handle(start("operator-1", "operator", "caller-1"))
    assert call.connected
    assert outcomes == ["connected"]


@pytest.mark.parametrize(
    "callback_enabled,hangup_during_callback_offer,emergency_during_callback",
    [(True, False, False), (False, False, False), (True, True, False), (True, False, True)],
)
async def test_real_call_session_handles_fake_ari_no_answer(
    line, callback_enabled, hangup_during_callback_offer, emergency_during_callback, monkeypatch
):
    """Проверяет полный локальный путь ARI → CallSession → dialogue → durable callback."""
    clock = FixedClock()
    handoff_store = MemoryHandoffStore()
    handoffs = HandoffService(
        handoff_store,
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=callback_enabled),
    )

    async def release_call_holds(_):
        return None

    booking = SimpleNamespace(
        clock=clock,
        clear_call_cache=lambda _: None,
        store=SimpleNamespace(release_call_holds=release_call_holds),
    )
    dialogue = DialogueService(
        booking,
        MemoryDialogueStore(),
        b"x" * 32,
        safety=RulesSafety(clock),
        handoffs=handoffs,
    )
    callback_final = "Что мне выпить, не могу дышать" if emergency_during_callback else "да"
    recognizer = SequenceRecognizer(
        ("соедините с оператором", "да", "79991234567", "в ближайшее время", callback_final)
    )
    sessions = []

    def new_session(call_id, channel):
        session = CallSession(
            call_id,
            channel,
            dialogue,
            Vad(),
            recognizer,
            Synthesizer(),
            today=lambda: date(2026, 10, 19),
            settings=VoiceSettings(
                _env_file=None,
                min_speech_ms=192,
                end_silence_ms=320,
                dictation_silence_ms=640,
                partial_ms=5000,
                silence_prompt_s=30,
                goodbye_hangup_s=0.05,
                wait_phrase_s=0.3,
            ),
        )
        sessions.append(session)
        return session

    line.sessions = new_session
    line.on_transfer_start = handoffs.transfer_started
    line.on_transfer = handoffs.transfer_result
    call_id = "full-t08-call"
    original_originate = line.ari.originate

    async def assert_card_precedes_dial(endpoint, args, timeout):
        request = await handoff_store.request_for_call(call_id)
        assert request is not None
        assert request.transfer_state.value == "attempting"
        assert request.identity_status == "unknown" and request.client_id is None
        return await original_originate(endpoint, args, timeout)

    monkeypatch.setattr(line.ari, "originate", assert_card_precedes_dial)
    await line.handle(start(call_id, "inbound"))
    session = line.calls[call_id].session
    await settled(lambda: not session.speaking())

    await session.feed(phrase(10))
    await settled(lambda: bool(recognizer.heard))
    assert recognizer.heard
    await settled(lambda: "operator-1" in line._operators)
    request = await handoff_store.request_for_call(call_id)
    assert request is not None and request.transfer_state.value == "attempting"
    assert request.priority == "P2"
    assert request.identity_status == "unknown" and request.client_id is None
    assert request.attempt_id is not None and handoff_store.mis_operations == {}
    originate = next(call for call in line.ari.calls if call[0] == "originate")
    assert originate[-1] == 30
    assert ("hangup", call_id) not in line.ari.calls

    await line.handle({"type": "ChannelDestroyed", "channel": {"id": "operator-1"}})
    await asyncio.sleep(0.05)
    request = await handoff_store.request_for_call(call_id)
    assert request is not None and request.transfer_state.value == "no_answer"
    assert session.turn_id == 2
    if not callback_enabled:
        await settled(lambda: "transfer.no_answer" in line.said)
        assert line.said[-1] == "transfer.no_answer"
        assert await handoff_store.get_callback_draft(request.request_id) is None
        assert await handoff_store.get_callback_task_by_request(request.request_id) is None
        await line.handle(end(call_id))
        assert call_id not in line.calls
        return

    await settled(lambda: len(session.synthesizer.spoken) > 1)
    assert any("согласны" in text.lower() for text in session.synthesizer.spoken), (
        session.synthesizer.spoken,
        session.ended,
        await handoff_store.get_callback_draft(request.request_id),
    )
    assert await handoff_store.request_for_call(call_id) is not None

    if hangup_during_callback_offer:
        await line.handle(end(call_id))
        await settled(lambda: call_id not in line.calls)
        assert session.ended
        assert await handoff_store.get_callback_draft(request.request_id) is None
        assert await handoff_store.get_callback_task_by_request(request.request_id) is None
        assert len(sessions) == 1
        return

    for words in (12, 14, 16, 18):
        previous_recognitions = len(recognizer.heard)
        await session.feed(phrase(words))
        await settled(lambda: len(recognizer.heard) > previous_recognitions)
        await settled(lambda: session._responder is not None and session._responder.done())
        if words < 18:
            await settled(lambda: not session.speaking())
    await asyncio.sleep(0.05)
    if emergency_during_callback:
        originates = [call for call in line.ari.calls if call[0] == "originate"]
        assert len(originates) == 2
        assert originates[1][-1] == 20
        assert "operator-1" in line._operators
        request = await handoff_store.request_for_call(call_id)
        assert request is not None
        assert request.route == "R-01" and request.priority == "P1"
        assert request.transfer_state.value == "attempting"
        assert await handoff_store.get_callback_draft(request.request_id) is None
        assert await handoff_store.get_callback_task_by_request(request.request_id) is None
        assert any("сто три" in spoken.lower() for spoken in session.synthesizer.spoken)
        await line.handle(end(call_id))
        return
    assert session.ended, (recognizer.heard, session.synthesizer.spoken)

    request = await handoff_store.request_for_call(call_id)
    assert request is not None
    task = await handoff_store.get_callback_task_by_request(request.request_id)
    assert task is not None, (recognizer.heard, session.synthesizer.spoken, request)
    assert task.contact_ciphertext
    assert task.contact_masked == "•••• 4567"
    assert handoffs.cipher.decrypt(task.contact_ciphertext.encode()) == b"+79991234567"
    assert session.turn_id == 6
    assert len(sessions) == 1

    await line.handle(end(call_id))
    assert call_id not in line.calls


@pytest.mark.parametrize("technical_client_id", [None, 999])
async def test_full_p1_call_session_and_mis_429_never_create_callback(line, technical_client_id):
    clock = FixedClock()
    handoff_store = MemoryHandoffStore()
    mis = SimpleNamespace(calls=[])

    async def create_escalation(payload):
        mis.calls.append(payload)
        raise MisError("RATE_LIMIT", retry_after=5)

    mis.create_escalation = create_escalation
    handoffs = HandoffService(
        handoff_store,
        mis,
        clock,
        b"x" * 32,
        HandoffSettings(
            enabled=True,
            callback_enabled=True,
            technical_client_id=technical_client_id,
        ),
    )

    async def release_call_holds(_):
        return None

    booking = SimpleNamespace(
        clock=clock,
        clear_call_cache=lambda _: None,
        store=SimpleNamespace(release_call_holds=release_call_holds),
    )
    dialogue = DialogueService(
        booking,
        MemoryDialogueStore(),
        b"x" * 32,
        safety=RulesSafety(clock),
        handoffs=handoffs,
    )
    recognizer = SequenceRecognizer(("Что мне выпить, не могу дышать",))
    sessions = []

    def new_session(call_id, channel):
        session = CallSession(
            call_id,
            channel,
            dialogue,
            Vad(),
            recognizer,
            Synthesizer(),
            today=lambda: date(2026, 10, 19),
            settings=VoiceSettings(
                _env_file=None,
                min_speech_ms=192,
                end_silence_ms=320,
                dictation_silence_ms=640,
                partial_ms=5000,
                silence_prompt_s=30,
                goodbye_hangup_s=0.05,
                wait_phrase_s=0.3,
            ),
        )
        sessions.append(session)
        return session

    line.sessions = new_session
    line.on_transfer_start = handoffs.transfer_started
    line.on_transfer = handoffs.transfer_result
    call_id = "full-t08-p1-no-client"
    await line.handle(start(call_id, "inbound"))
    session = line.calls[call_id].session
    await settled(lambda: not session.speaking())
    await session.feed(phrase(10))
    await settled(lambda: bool(recognizer.heard))
    await settled(lambda: "operator-1" in line._operators)

    request = await handoff_store.request_for_call(call_id)
    assert request is not None
    assert (request.route, request.priority, request.identity_status, request.client_id) == (
        "R-01",
        "P1",
        "unknown",
        None,
    )
    originate = next(call for call in line.ari.calls if call[0] == "originate")
    assert originate[-1] == 20
    assert any("сто три" in spoken.lower() for spoken in sessions[0].synthesizer.spoken)

    await line.handle({"type": "ChannelDestroyed", "channel": {"id": "operator-1"}})
    await settled(lambda: ("hangup", call_id) in line.ari.calls)
    request = await handoff_store.request_for_call(call_id)
    assert request is not None and request.transfer_state.value == "no_answer"
    if technical_client_id is None:
        assert request.mis_state.value == "manual"
        assert request.mis_operation_id is None
        assert handoff_store.mis_operations == {}
        assert mis.calls == []
    else:
        assert request.mis_state.value == "pending"
        assert request.mis_operation_id is not None
        # RATE_LIMIT is handled as a retryable operation, so this method's
        # return count includes only successful deliveries.
        assert await handoffs.deliver_mis_once() == 0
        operation = await handoff_store.get_mis_operation(request.mis_operation_id)
        assert operation.state.value == "pending" and operation.reason == "RATE_LIMIT"
        assert len(mis.calls) == 1
        assert await handoffs.deliver_mis_once() == 0
        assert len(mis.calls) == 1
    assert handoff_store.callback_drafts == {}
    assert handoff_store.callback_tasks == {}
    assert len(sessions) == 1
    await line.handle(end(call_id))
