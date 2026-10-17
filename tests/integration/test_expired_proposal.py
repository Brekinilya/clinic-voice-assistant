"""Позднее согласие требует нового предложения, а не запросов к МИС."""

from datetime import timedelta

import pytest
from fixture_bridge import pipeline, turn

from app.booking.models import ActionState, Approval

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"


async def reserve(client):
    await turn(client, 1, "Запишите к терапевту на пятницу")
    await turn(client, 2, IDENTITY)
    result = await turn(client, 3, "В десять тридцать")
    assert result["phase"] == "held"
    return result


@pytest.mark.parametrize("delay", [90, 121])
@pytest.mark.parametrize("answer", ["Да", "Да, пожалуйста"])
async def test_late_agreement_explains_expiry_without_mis_and_replays(delay, answer):
    async with pipeline() as (client, service, emulator, clock, *_):
        proposal = (await reserve(client))["proposal"]
        clock.value += timedelta(seconds=delay)
        requests = []

        async def record(request):
            requests.append(request.method)

        service.booking.mis._client.event_hooks["request"].append(record)
        result = await turn(client, 4, answer)
        assert result["code"] == "PROPOSAL_EXPIRED"
        assert result["phase"] == "identified"
        assert result["proposal"] is None and not result["options"]
        assert "истёк" in " ".join(result["utterances"])
        assert "день или время" in " ".join(result["utterances"])
        state = await service.store.inspect("scenario")
        assert state.authorization is None and state.misunderstandings == 0
        assert await turn(client, 4, answer) == result
        assert requests == [] and emulator.write_count == 0
        action = await service.booking.store.get(proposal["action_id"])
        assert action.state not in {ActionState.HELD, ActionState.BOOKED}


async def test_agreement_before_expiry_still_books():
    async with pipeline() as (client, _, emulator, clock, *_):
        await reserve(client)
        clock.value += timedelta(seconds=89)
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


async def test_new_date_after_expiry_is_processed_and_requires_new_agreement():
    async with pipeline() as (client, _, emulator, clock, *_):
        old = (await reserve(client))["proposal"]
        clock.value += timedelta(seconds=90)
        result = await turn(client, 4, "Да, но в понедельник")
        assert result["phase"] == "offered" and result["options"]
        assert result["proposal"] is None and result["code"] != "PROPOSAL_EXPIRED"
        assert all("2026-10-05" in item["scheduled_at"] for item in result["options"])
        assert emulator.write_count == 0
        selected = await turn(client, 5, "Первый вариант")
        assert selected["phase"] == "held" and selected["proposal"]["action_id"] != old["action_id"]
        assert emulator.write_count == 0
        assert (await turn(client, 6, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


@pytest.mark.parametrize("state", [ActionState.COMMITTING, ActionState.RECONCILING])
async def test_sent_operation_remains_pending_after_hold_expiry(state):
    async with pipeline() as (client, service, emulator, clock, *_):
        proposal = (await reserve(client))["proposal"]
        action, claimed = await service.booking.store.claim(
            Approval(
                call_id="scenario",
                action_id=proposal["action_id"],
                revision=proposal["revision"],
                turn_id=4,
                is_final=True,
                permitted=True,
            ),
            clock.now(),
        )
        assert claimed
        if state == ActionState.RECONCILING:
            await service.booking.store.finish(action.action_id, state)
        clock.value += timedelta(seconds=121)
        result = await turn(client, 4, "Да")
        assert result["code"] == "RECONCILING" and result["phase"] == "reconciling"
        assert (await service.booking.store.get(action.action_id)).state == state
        assert emulator.write_count == 0
