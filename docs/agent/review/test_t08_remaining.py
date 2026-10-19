"""Оставшиеся красные проверки интеграции; не часть принятого main."""

import asyncio
from types import SimpleNamespace

import pytest

from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.models import Phase, TransferRequest, Turn
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.service import HandoffService, HandoffSettings
from tests.test_handoffs_r01 import Clock, NoMis, prepared_callback


def system():
    async def release(_):
        pass

    clock = Clock()
    booking = SimpleNamespace(
        clock=clock,
        clear_call_cache=lambda _: None,
        store=SimpleNamespace(release_call_holds=release),
    )
    state_store = MemoryDialogueStore()
    handoffs = HandoffService(
        MemoryHandoffStore(),
        NoMis(),
        clock,
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    dialogue = DialogueService(
        booking, state_store, b"x" * 32, safety=RulesSafety(clock), handoffs=handoffs
    )
    return dialogue, handoffs


@pytest.mark.asyncio
async def test_patient_text_operator_request_creates_local_card():
    dialogue, handoffs = system()
    reply = await dialogue.handle(
        "text-review", Turn(turn_id=1, text="соедините с оператором"), source="text"
    )
    assert reply.phase == Phase.HANDOFF
    # В текстовом канале событие ARI transfer_started не придёт.
    assert await handoffs.store.request_for_call("text-review") is not None


@pytest.mark.asyncio
async def test_declined_callback_persists_terminal_dialogue_state():
    dialogue, handoffs = system()
    call = "terminal-review"
    await dialogue.store.claim(call, 1, "handoff-seed")
    await dialogue.store.preempt(call, 1, TransferRequest(route="R-08", priority="P3"))
    event = SimpleNamespace(
        attempt_id="terminal-attempt", call_id=call, route="R-08", priority="P3"
    )
    await handoffs.transfer_started(event)
    await handoffs.transfer_result(SimpleNamespace(**vars(event), outcome="no_answer", reason=None))
    await dialogue.start_callback(call, 1, "no_answer")
    result = await dialogue.handle(call, Turn(turn_id=2, text="нет"))
    assert result.phase == Phase.ENDED
    assert (await dialogue.store.inspect(call)).phase == Phase.ENDED


@pytest.mark.asyncio
async def test_new_final_text_revokes_pending_confirmation_without_skipping_turn(monkeypatch):
    """Даже отклонённая как несвоевременная новая фраза отзывает старое согласие."""
    handoffs, store, clock, event, request = await prepared_callback()
    dialogue_store = MemoryDialogueStore()
    await dialogue_store.preempt(event.call_id, 1, TransferRequest(route="R-08", priority="P3"))
    dialogue = DialogueService(
        None, dialogue_store, b"x" * 32, safety=RulesSafety(clock), handoffs=handoffs
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.create_callback_task

    async def delayed(task, **authorization):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(task, **authorization)

    monkeypatch.setattr(store, "create_callback_task", delayed)
    writer = asyncio.create_task(dialogue.handle(event.call_id, Turn(turn_id=5, text="да")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await dialogue.handle(event.call_id, Turn(turn_id=6, text="79990000000"))
    finally:
        release.set()
        await writer
    assert await store.get_callback_task_by_request(request.request_id) is None
