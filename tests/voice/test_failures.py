"""Границы сбоев голоса: общий перехват только через failure(), в журнале нет текста ошибки."""

import ast
import logging
from pathlib import Path

from app.voice.failures import failure
from tests.voice.fakes import Dialogue, Recognizer
from tests.voice.test_session import answered, phrase, session, until

VOICE = Path(__file__).resolve().parents[2] / "app" / "voice"
SECRET = "Иванова Мария, 12.03.1978"


def test_failure_writes_event_call_and_type_without_the_message(caplog):
    with caplog.at_level(logging.INFO, logger="app.voice.failures"):
        failure("voice.dialogue_failed", RuntimeError(SECRET), call="call-1")
    [record] = caplog.records
    assert record.getMessage() == "voice.dialogue_failed звонок=call-1 ошибка=RuntimeError"
    assert record.exc_info is None
    assert SECRET not in caplog.text


def test_trace_is_kept_only_for_infrastructure_or_debug(caplog):
    with caplog.at_level(logging.INFO, logger="app.voice.failures"):
        failure("voice.ari_events_failed", ConnectionError("ari"), trace=True)
    assert caplog.records[-1].exc_info is not None
    with caplog.at_level(logging.DEBUG, logger="app.voice.failures"):
        failure("voice.asr_failed", ValueError(SECRET))
    assert caplog.records[-1].exc_info is not None


async def test_failing_dialogue_is_logged_without_patient_text_and_the_call_goes_on(caplog):
    class Broken(Dialogue):
        async def handle(self, call_id, turn):
            if turn.is_final:
                raise RuntimeError(SECRET)
            return await super().handle(call_id, turn)

    call, channel = session(Broken(lambda turn: None), Recognizer({10: "Запишите к терапевту"}))
    try:
        with caplog.at_level(logging.INFO, logger="app.voice"):
            await answered(call)
            await call.feed(phrase(10))
            await until(
                lambda: any("voice.dialogue_failed" in r.getMessage() for r in caplog.records)
            )
            await until(lambda: channel.of("end"))
        assert SECRET not in caplog.text
        # Сбой диалога — безопасный перевод на регистратора, а не обрыв звонка
        [end] = channel.of("end")
        assert (end["reason"], end["route"], end["cause"]) == (
            "transfer",
            "R-06",
            "dialogue_unavailable",
        )
    finally:
        await call.close()


def broad(handler: ast.ExceptHandler) -> bool:
    names = []
    if handler.type is None:
        return True
    for node in handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]:
        names.append(node.id if isinstance(node, ast.Name) else getattr(node, "attr", ""))
    return bool({"Exception", "BaseException"} & set(names))


def silent_handlers(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler) and broad(node):
            body = [n for statement in node.body for n in ast.walk(statement)]
            logged = any(
                isinstance(n, ast.Call) and getattr(n.func, "id", None) == "failure" for n in body
            )
            if not logged and not any(isinstance(n, ast.Raise) for n in body):
                lines.append(node.lineno)
    return lines


def test_guard_catches_a_silent_broad_except():
    source = """
try:
    work()
except (ValueError, Exception):
    pass
except BaseException as error:
    failure("voice.x_failed", error)
"""
    assert silent_handlers(source) == [4]


def test_every_broad_except_in_voice_goes_through_failure_or_reraises():
    silent = [
        f"{path.name}:{line}"
        for path in sorted(VOICE.glob("*.py"))
        for line in silent_handlers(path.read_text("utf-8"))
    ]
    assert silent == []
