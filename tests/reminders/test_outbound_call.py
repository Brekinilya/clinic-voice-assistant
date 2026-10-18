"""T11: исходящий звонок через телефонию и голосовую сессию; диалог — сценарный фейк порта."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.reminders.executor import ReminderExecutor
from app.reminders.marks import ReminderMarks, confirmation_line
from app.reminders.ports import DialRejected
from app.voice.session import CallSession
from app.voice.telephony import AriError, Telephony, TelephonySettings
from tests.reminders.test_reminders import CALENDAR, PHONE, Stand, ready
from tests.voice.fakes import Channel, Recognizer, Synthesizer, Vad, phrase, reply
from tests.voice.test_session import SETTINGS, TODAY, until
from tests.voice.test_telephony import PCM, Ari, Session, free_port, start


def result(context, outcome, mis_status="not_required"):
    return SimpleNamespace(attempt_id=context.attempt_id, outcome=outcome, mis_status=mis_status)


class ReminderDialogue:
    """Сценарий С-10 на ключевых фразах по порту app/dialogue/outbound — только для шлюза."""

    def __init__(self, marks: ReminderMarks | None = None, fail: bool = False):
        self.marks = marks
        self.fail = fail
        self.contexts = {}
        self.phase = {}
        self.events = []
        self.turns = []
        self.started = []
        self.ended = 0

    async def open_outbound(self, call_id, context):
        if self.fail:
            raise RuntimeError("диалог недоступен")
        self.contexts[call_id] = context
        self.phase[call_id] = "verify"
        return reply(f"Здравствуйте! Я говорю с {context.addressee}?", expect="yes_no")

    async def outbound_event(self, call_id, event):
        self.events.append(event)
        context = self.contexts[call_id]
        return reply(
            "Пожалуйста, перезвоните в регистратуру.",
            phase="ended",
            reminder_result=result(context, "VOICEMAIL"),
        )

    async def input_started(self, call_id, turn_id):
        self.started.append(turn_id)

    async def handle(self, call_id, turn):
        if not turn.is_final:
            return reply("", kind="partial")
        self.turns.append((turn.turn_id, turn.text))
        phase, context = self.phase[call_id], self.contexts[call_id]
        if phase == "verify" and turn.text == "Да":
            self.phase[call_id] = "details"
            return reply("Напоминаю о записи к терапевту. Вы придёте?", expect="yes_no")
        if phase == "verify":
            return reply(
                "Передайте, пожалуйста, просьбу перезвонить.",
                phase="ended",
                reminder_result=result(context, "WRONG_PERSON"),
            )
        if turn.text == "Да, приду":
            status = "applied" if self.marks is None else await self.marks.confirm(context)
            return reply(
                "Отлично, ждём вас.",
                phase="ended",
                reminder_result=result(context, "CONFIRMED", status),
            )
        return reply("Не поняла.", code="NOT_UNDERSTOOD")

    async def end(self, call_id):
        self.ended += 1


def outbound_session(dialogue, context, outcomes, **kwargs):
    async def report(outcome, mis_status):
        outcomes.append((outcome, mis_status))

    channel = Channel()
    texts = {7: "Да", 8: "Да, приду", 9: "Нет, это муж"}
    call = CallSession(
        "out-1",
        channel,
        dialogue,
        Vad(),
        Recognizer(texts),
        Synthesizer(),
        today=lambda: TODAY,
        settings=SETTINGS.model_copy(update={"partial_ms": 5000}),
        outbound=context,
        on_outcome=report,
        **kwargs,
    )
    return call, channel


class Context:
    attempt_id = "att-1"
    fence = 1
    addressee = "Иван Иванович"
    client_id = 3


class ChannelAri(Ari):
    async def originate(self, endpoint, args, timeout, channel_id=None):
        self.calls.append(("originate", endpoint, tuple(args), timeout, channel_id))
        return channel_id or "operator-1"


async def spoken(call, channel, frames, count):
    await call.feed(phrase(frames))
    await until(lambda: len(channel.of("reply")) >= count)
    await until(lambda: not call.speaking())


# 1. Голосовая сессия в исходящем режиме


async def test_outbound_session_opens_with_the_dialogue_and_reports_outcome_once():
    dialogue, outcomes = ReminderDialogue(), []
    call, channel = outbound_session(dialogue, Context(), outcomes)
    await call.start()
    assert channel.of("ready")[0]["direction"] == "outbound"
    assert channel.said()[0] == "Здравствуйте! Я говорю с Иван Иванович?"
    await until(lambda: not call.speaking())
    await spoken(call, channel, 7, 2)
    await spoken(call, channel, 8, 3)
    await until(lambda: channel.of("end"))
    # Открытие — реплика 1, речь пациента начинается с 2
    assert dialogue.turns == [(2, "Да"), (3, "Да, приду")]
    await call.close()
    await call.close()
    assert outcomes == [("CONFIRMED", "applied")]


async def test_wrong_person_ends_without_visit_details():
    dialogue, outcomes = ReminderDialogue(), []
    call, channel = outbound_session(dialogue, Context(), outcomes)
    await call.start()
    await until(lambda: not call.speaking())
    await spoken(call, channel, 9, 2)
    await until(lambda: channel.of("end"))
    await call.close()
    assert outcomes == [("WRONG_PERSON", "not_required")]
    assert not any("терапевт" in text for text in channel.said())


async def test_hangup_mid_conversation_reports_no_outcome():
    dialogue, outcomes = ReminderDialogue(), []
    call, channel = outbound_session(dialogue, Context(), outcomes)
    await call.start()
    await until(lambda: not call.speaking())
    await spoken(call, channel, 7, 2)
    await call.close()
    assert outcomes == [(None, None)]


async def test_answering_machine_gets_the_neutral_message():
    dialogue, outcomes = ReminderDialogue(), []
    call, channel = outbound_session(dialogue, Context(), outcomes, machine=True)
    await call.start()
    [event] = dialogue.events
    assert (event.attempt_id, event.turn_id, event.kind) == ("att-1", 2, "voicemail")
    # Приветствие с именем автоответчику не звучит
    assert channel.said() == ["Пожалуйста, перезвоните в регистратуру."]
    await until(lambda: channel.of("end"))
    await call.close()
    assert outcomes == [("VOICEMAIL", "not_required")]


async def test_dialogue_that_cannot_open_hangs_up_quietly():
    dialogue, outcomes = ReminderDialogue(fail=True), []
    call, channel = outbound_session(dialogue, Context(), outcomes)
    await call.start()
    await until(lambda: channel.of("end"))
    assert channel.of("end")[0]["cause"] == "outbound_unavailable" and not channel.said()
    await call.close()
    assert outcomes == [(None, None)]


# 2. Телефония: originate, ответ, неответ


class Reminders:
    def __init__(self):
        self.answered_ids = []
        self.results = []

    async def answered(self, attempt_id):
        self.answered_ids.append(attempt_id)

    async def finished(self, attempt_id, result, reason=None, mis_status=None):
        self.results.append((attempt_id, result, reason, mis_status))
        return True


@pytest.fixture
async def line():
    port = free_port()
    settings = TelephonySettings(
        _env_file=None, media_host="gateway.test", media_bind="127.0.0.1", media_ports=(port, port)
    )
    made = []

    def outbound(call_id, channel, context, machine, done):
        session = Session()
        session.context, session.machine, session.done = context, machine, done
        made.append(session)
        return session

    async def say(template):
        return PCM, 8000

    telephony = Telephony(
        ChannelAri(),
        settings,
        lambda call_id, channel: Session(),
        say,
        lambda pcm, s, d: pcm,
        outbound=outbound,
        reminders=Reminders(),
    )
    telephony.made = made
    yield telephony
    await telephony.close()


async def test_answered_outbound_call_is_bridged_without_answering_it(line):
    context = object()
    await line.call_out("att-1", PHONE, context, "reminder-att-1")
    assert line.ari.calls[-1] == (
        "originate",
        PHONE,
        ("outbound", "att-1"),
        30,
        "reminder-att-1",
    )
    line.ari.calls.clear()
    await line.handle(start("reminder-att-1", "outbound", "att-1"))
    assert ("answer", "reminder-att-1") not in line.ari.calls
    assert ("add", "bridge-1", "reminder-att-1", "media-1") in line.ari.calls
    [session] = line.made
    assert session.context is context and not session.machine and session.started
    assert line.reminders.answered_ids == ["att-1"]


async def test_machine_answer_is_passed_to_the_session(line):
    await line.call_out("att-1", PHONE, object(), "reminder-att-1")
    await line.handle(start("reminder-att-1", "outbound", "att-1", "MACHINE"))
    assert line.made[0].machine


@pytest.mark.parametrize("cause, result", [(19, "NO_ANSWER"), (17, "NO_ANSWER"), (20, "FAILED")])
async def test_unanswered_call_is_reported_once(line, cause, result):
    await line.call_out("att-1", PHONE, object())
    for _ in range(2):
        await line.handle(
            {"type": "ChannelDestroyed", "channel": {"id": "operator-1"}, "cause": cause}
        )
    assert line.reminders.results == [("att-1", result, f"q850_{cause}", None)]
    assert not line.made


async def test_unknown_outbound_start_is_hung_up(line):
    await line.handle(start("stranger-1", "outbound", "att-404"))
    assert line.ari.calls == [("hangup", "stranger-1")] and not line.made


async def test_rejected_originate_raises_dial_rejected(line):
    async def rejected(endpoint, args, timeout, channel_id=None):
        raise AriError("POST channels: 400")

    line.ari.originate = rejected
    with pytest.raises(DialRejected):
        await line.call_out("att-1", PHONE, object())


async def test_hangup_after_answer_reports_through_the_session(line):
    await line.call_out("att-1", PHONE, object())
    await line.handle(start("operator-1", "outbound", "att-1"))
    session = line.made[0]
    original = session.close

    async def close():
        await original()
        await session.done("CONFIRMED", "applied")

    session.close = close
    await line.handle({"type": "StasisEnd", "channel": {"id": "operator-1"}})
    await line.handle({"type": "ChannelDestroyed", "channel": {"id": "operator-1"}, "cause": 16})
    assert line.reminders.results == [("att-1", "CONFIRMED", None, "applied")]


# 3. Сквозной путь: отбор → вызов → разговор → отметка в МИС → исход задания


async def test_reminder_end_to_end_confirms_in_the_mis_comment():
    stand = Stand()
    try:
        visit = await ready(stand)
        marks = ReminderMarks(stand.mis, stand.store, stand.clock, owner="vm-1")
        dialogue = ReminderDialogue(marks)
        port = free_port()
        settings = TelephonySettings(
            _env_file=None, media_host="gw.test", media_bind="127.0.0.1", media_ports=(port, port)
        )
        sessions = []

        def outbound(call_id, channel, context, machine, done):
            session = CallSession(
                call_id,
                channel,
                dialogue,
                Vad(),
                Recognizer({7: "Да", 8: "Да, приду"}),
                Synthesizer(),
                today=lambda: date(2026, 10, 19),
                settings=SETTINGS.model_copy(update={"partial_ms": 5000}),
                outbound=context,
                machine=machine,
                on_outcome=done,
            )
            sessions.append(session)
            return session

        async def say(template):
            return PCM, 8000

        telephony = Telephony(
            ChannelAri(), settings, None, say, lambda pcm, s, d: pcm, outbound=outbound
        )
        executor = ReminderExecutor(
            stand.store,
            stand.mis,
            CALENDAR,
            stand.clock,
            telephony,
            owner="vm-1",
            allowed_endpoints={PHONE},
        )
        telephony.reminders = executor
        await executor.tick()
        [(_, endpoint, (_, attempt_id), _, channel_id)] = [
            c for c in telephony.ari.calls if c[0] == "originate"
        ]
        assert endpoint == PHONE
        await telephony.handle(start(channel_id, "outbound", attempt_id))
        call = sessions[0]
        # Звук подаём сами: заполнение пауз тишиной из RTP разорвало бы подставную «фразу»
        for task in list(telephony.calls[channel_id].tasks):
            task.cancel()
        await until(lambda: not call.speaking())
        for frames, count in ((7, 2), (8, 3)):
            await call.feed(phrase(frames))
            await until(lambda: len(dialogue.turns) >= count - 1)
            await until(lambda: not call.speaking())
        await until(lambda: call.finished.is_set())
        await telephony.handle({"type": "ChannelDestroyed", "channel": {"id": channel_id}})
        job = next(iter(stand.store.jobs.values()))
        assert (job.state, job.outcome, job.mis_status) == ("done", "CONFIRMED", "applied")
        assert stand.emulator.clients["demo"][3].comment == confirmation_line(
            visit.id, visit.scheduled_at
        )
        assert executor.active is None
        await telephony.close()
    finally:
        await stand.http.aclose()
        await asyncio.sleep(0)
