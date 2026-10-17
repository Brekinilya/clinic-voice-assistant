"""Начало речи отзывает разрешение до МИС; те же переходы памяти и PostgreSQL."""

import asyncio

import pytest
from fixture_bridge import pipeline, turn

from app.booking.models import ActionState
from app.dialogue.expectation import expected_answer
from app.dialogue.models import DialogueState, Phase, Turn, reply

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def voice_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


async def reserve(client):
    first = await turn(client, 1, "Запишите к терапевту на пятницу")
    second = await turn(client, 2, IDENTITY)
    third = await turn(client, 3, "В десять тридцать")
    assert [r["expect"] for r in (first, second, third)] == ["full_name", "choice", "yes_no"]
    return third


@pytest.mark.parametrize(
    "phase,expect",
    [
        (Phase.IDENTIFYING, "full_name"),
        (Phase.OFFERED, "choice"),
        (Phase.HELD, "yes_no"),
        (Phase.CANCEL_CHOOSING, "choice"),
        (Phase.MOVE_OFFERED, "choice"),
        (Phase.CANCEL_CONFIRM, "yes_no"),
        (Phase.MOVE_FINISH_CONFIRM, "yes_no"),
        (Phase.BOOKED, "yes_no"),
        (Phase.HANDOFF, "free"),
        (Phase.ENDED, "free"),
    ],
)
def test_reply_expect_uses_state_and_same_mapping_as_nlu(phase, expect):
    state = DialogueState(call_id="voice", phase=phase)
    assert reply(state, 1).expect == expected_answer(state) == expect
    boundary = state.model_copy(update={"medical_boundary_pending": True})
    assert reply(boundary, 1).expect == "yes_no"


async def test_input_start_is_idempotent_even_after_final_text(voice_engine):
    async with pipeline(pg_engine=voice_engine) as (client, service, *_):
        await reserve(client)
        first = await service.input_started("scenario", 4)
        repeated = await service.input_started("scenario", 4)
        assert repeated == first and first.kind == "partial"
        text = Turn(turn_id=4, text="Да")
        state = await service.store.observe("scenario", 4, service._digest("scenario", text))
        claim = await service.store.claim("scenario", 4, service._digest("scenario", text))
        assert await service.store.authorize("scenario", claim.state.version, state.input_epoch, 4)
        before = await service.store.inspect("scenario")
        await service.input_started("scenario", 4)
        await service.input_started("scenario", 3)
        assert await service.store.inspect("scenario") == before
        await service.input_started("scenario", 5)
        after = await service.store.inspect("scenario")
        assert after.authorization is None and after.input_epoch > before.input_epoch


@pytest.mark.parametrize("mode", ["start", "red_flag", "no"])
async def test_new_speech_during_preflight_prevents_unsent_post(voice_engine, mode):
    async with pipeline(pg_engine=voice_engine) as (client, service, emulator, *_):
        reserved = await reserve(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.booking._preflight

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        service.booking._preflight = slow
        confirmation = asyncio.create_task(turn(client, 4, "Да"))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            response = await client.post("/api/dialog/scenario/input-started", json={"turn_id": 5})
            assert response.status_code == 200 and response.json()["code"] == "INPUT_STARTED"
            if mode != "start":
                partial = await service.partial(
                    "scenario",
                    Turn(
                        turn_id=5,
                        text="У меня сильно давит в груди" if mode == "red_flag" else "Нет",
                        is_final=False,
                    ),
                )
                assert (
                    partial.code == "EMERGENCY" if mode == "red_flag" else partial.kind == "partial"
                )
        finally:
            release.set()
        result = await asyncio.wait_for(confirmation, 3)
        assert emulator.write_count == 0 and result["code"] != "BOOKED"
        assert (
            await service.booking.store.get(reserved["proposal"]["action_id"])
        ).state != ActionState.BOOKED
        state = await service.store.inspect("scenario")
        if mode == "red_flag":
            assert state.phase == Phase.HANDOFF and state.transfer.route == "R-01"
        else:
            assert state.authorization is None


async def test_cancelled_asr_turn_can_be_skipped_only_after_start_event(voice_engine):
    async with pipeline(pg_engine=voice_engine) as (client, service, emulator, *_):
        await service.input_started("scenario", 1)
        await service.input_started("scenario", 2)
        result = await turn(client, 2, "Хочу записаться к терапевту")
        assert result["expect"] == "full_name" and result["phase"] == "identifying"
        assert emulator.write_count == 0
        response = await client.post(
            "/api/dialog/scenario/turn", json={"turn_id": 4, "text": IDENTITY}
        )
        assert response.status_code == 409 and response.json()["detail"] == "TURN_OUT_OF_ORDER"


async def test_start_while_pending_accounts_for_cancelled_recognition_turns(voice_engine):
    async with pipeline(pg_engine=voice_engine) as (client, service, emulator, *_):
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.booking.identify

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        service.booking.identify = slow
        await turn(client, 1, "Хочу записаться к терапевту")
        old = asyncio.create_task(turn(client, 2, IDENTITY))
        await asyncio.wait_for(entered.wait(), 3)
        await service.input_started("scenario", 3)
        await service.input_started("scenario", 4)
        release.set()
        assert (await old)["code"] == "TURN_INTERRUPTED"
        current = await turn(client, 4, IDENTITY)
        assert current["phase"] == "offered" and emulator.write_count == 0


async def test_start_cannot_reset_a_sent_mutation_or_terminal_call(voice_engine):
    async with pipeline(pg_engine=voice_engine) as (client, service, emulator, *_):
        reserved = await reserve(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.booking.mis.create_visit

        async def slow(request):
            visit = await original(request)
            entered.set()
            await release.wait()
            return visit

        service.booking.mis.create_visit = slow
        old = asyncio.create_task(turn(client, 4, "Да"))
        await asyncio.wait_for(entered.wait(), 3)
        await service.input_started("scenario", 5)
        emergency = await service.partial(
            "scenario", Turn(turn_id=5, text="У меня сильно давит в груди", is_final=False)
        )
        assert emergency.code == "EMERGENCY"
        release.set()
        await old
        action = await service.booking.store.get(reserved["proposal"]["action_id"])
        assert action.state == ActionState.BOOKED and emulator.write_count == 1
        # Уже отправленную запись не считаем отменённой и не повторяем.
        assert (await service.input_started("scenario", 6)).transfer.route == "R-01"
        await service.end("scenario")
        assert (await service.input_started("scenario", 7)).code == "CALL_ENDED"
        assert emulator.write_count == 1


async def test_partial_rejects_final_and_http_rejects_invalid_start_ids():
    async with pipeline() as (client, service, emulator, *_):
        with pytest.raises(ValueError):
            await service.partial("scenario", Turn(turn_id=1, text="Да"))
        for value in (0, -1, True, "1"):
            result = await client.post(
                "/api/dialog/scenario/input-started", json={"turn_id": value}
            )
            assert result.status_code == 422
        assert emulator.write_count == 0


async def test_gateway_start_revokes_old_yes_before_recognition_finishes(voice_engine):
    from app.voice.session import CallSession
    from tests.voice.fakes import Channel, Recognizer, Synthesizer, Vad

    async with pipeline(pg_engine=voice_engine) as (client, service, emulator, clock, *_):
        await reserve(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.booking._preflight

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        service.booking._preflight = slow
        confirmation = asyncio.create_task(turn(client, 4, "Да"))
        await asyncio.wait_for(entered.wait(), 3)
        recognizer = Recognizer({}, delay=1)
        call = CallSession(
            "scenario",
            Channel(),
            service,
            Vad(),
            recognizer,
            Synthesizer(),
            today=lambda: clock.now().date(),
        )
        call.turn_id = 4
        try:
            await asyncio.wait_for(call._phrase_started(), 3)
            state = await service.store.inspect("scenario")
            assert state.observed_turn_id == 5 and state.authorization is None
            assert recognizer.heard == []
        finally:
            release.set()
        result = await asyncio.wait_for(confirmation, 3)
        assert result["code"] != "BOOKED" and emulator.write_count == 0
        await call.close()
