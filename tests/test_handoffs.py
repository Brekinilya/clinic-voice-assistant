from datetime import datetime
from types import SimpleNamespace

import pytest

from app.clock import CLINIC_TIMEZONE
from app.dialogue.models import Turn
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.models import TransferState
from app.handoffs.service import HandoffService, HandoffSettings
from app.mis.models import EscalationCreate


class Clock:
    def now(self):
        return datetime(2026, 10, 19, 10, 0, tzinfo=CLINIC_TIMEZONE)


class NoMis:
    async def create_escalation(self, payload):
        raise AssertionError("unknown caller must not create an MIS escalation")


def test_escalation_payload_matches_the_primary_source_shape():
    request = EscalationCreate(client_id=42, description="handoff marker")
    assert request.body() == {"client_id": 42, "description": "handoff marker"}


def test_escalation_payload_rejects_non_openapi_control_characters():
    request = EscalationCreate(client_id=42, description="bad\x01text")
    with pytest.raises(ValueError):
        request.body()


@pytest.mark.asyncio
async def test_transfer_attempt_is_journaled_and_replayed_once():
    store = MemoryHandoffStore()
    service = HandoffService(store, NoMis(), Clock(), b"x" * 32, HandoffSettings(enabled=True))
    event = SimpleNamespace(
        attempt_id="attempt-1", call_id="channel-1", route="R-08", priority="P3"
    )

    await service.transfer_started(event)
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )

    request = await store.request_for_call("channel-1")
    assert request is not None
    assert request.transfer_state == TransferState.NO_ANSWER
    assert len(store.requests) == len(store.attempts) == 1
    assert not store.mis_operations


@pytest.mark.asyncio
async def test_callback_requires_consent_and_explicit_final_confirmation():
    store = MemoryHandoffStore()
    service = HandoffService(
        store,
        NoMis(),
        Clock(),
        b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    request = await service.ensure_request("channel-2", "R-08", "P3")
    await service.transfer_started(
        SimpleNamespace(
            attempt_id="attempt-2", call_id="channel-2", route="R-08", priority="P3"
        )
    )
    await service.transfer_result(
        SimpleNamespace(
            attempt_id="attempt-2", call_id="channel-2", route="R-08", priority="P3",
            outcome="no_answer", reason="timeout",
        )
    )

    offer = await service.start_callback("channel-2", 1, "no_answer")
    assert offer.code == "CALLBACK_OFFERED"
    assert await store.get_callback_task_by_request(request.request_id) is None

    await service.callback_turn("channel-2", Turn(turn_id=2, text="да"))
    await service.callback_turn("channel-2", Turn(turn_id=3, text="79991234567"))
    await service.callback_turn("channel-2", Turn(turn_id=4, text="в ближайшее время"))
    queued = await service.callback_turn("channel-2", Turn(turn_id=5, text="да"))

    task = await store.get_callback_task_by_request(request.request_id)
    assert queued.code == "CALLBACK_QUEUED"
    assert task is not None
    assert task.contact_masked.endswith("4567")
    assert service.reveal_contact(task) == "+79991234567"
    assert task.next_at == Clock().now()


@pytest.mark.asyncio
async def test_declined_callback_leaves_no_task_or_draft():
    store = MemoryHandoffStore()
    service = HandoffService(
        store, NoMis(), Clock(), b"x" * 32,
        HandoffSettings(enabled=True, callback_enabled=True),
    )
    request = await service.ensure_request("channel-3", "R-08", "P2")
    event = SimpleNamespace(
        attempt_id="attempt-3", call_id="channel-3", route="R-08", priority="P2"
    )
    await service.transfer_started(event)
    await service.transfer_result(
        SimpleNamespace(**vars(event), outcome="no_answer", reason="timeout")
    )
    await service.start_callback("channel-3", 1, "no_answer")
    result = await service.callback_turn("channel-3", Turn(turn_id=2, text="нет"))

    assert result.code == "CALLBACK_DECLINED"
    assert await store.get_callback_draft(request.request_id) is None
    assert await store.get_callback_task_by_request(request.request_id) is None
