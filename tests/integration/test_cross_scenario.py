from app.dialogue.models import Reply
from tests.scenarios.context_matrix import patient_steps
from tests.scenarios.runtime import ScenarioRuntime
from tests.scenarios.scenario_plan import load_scenarios


def dialog_by_label(label):
    return next(
        dialog
        for scenario in load_scenarios()
        for dialog in scenario.dialogs
        if dialog.label == label
    )


async def advance(client, call_id, turn_id, text):
    response = await client.post(
        f"/api/dialog/{call_id}/turn",
        json={"turn_id": turn_id, "text": text, "is_final": True},
    )
    assert response.status_code == 200, response.text
    return Reply.model_validate(response.json())


async def test_goodbye_at_booking_confirmation_never_confirms_the_booking():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    records_before = len(runtime.snapshot())
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:4], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])

        prompt = await advance(client, runtime.call_id, 5, "Спасибо, до свидания.")
        assert prompt.code == "END_CONFIRMATION_REQUIRED"
        assert prompt.phase == "held"

        ended = await advance(client, runtime.call_id, 6, "Да.")
        assert ended.phase == "ended"
        assert len(runtime.snapshot()) == records_before


async def test_more_options_returns_a_new_page_without_repeating_slots():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:3], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])
        before = await runtime.dialogue_store.inspect(runtime.call_id)

        result = await advance(client, runtime.call_id, 4, "А какие ещё есть варианты?")
        assert result.code in {"MORE_OPTIONS", "NO_MORE_OPTIONS"}
        if result.code == "MORE_OPTIONS":
            previous = {(slot.doctor_id, slot.scheduled_at) for slot in before.options}
            assert all(
                (slot.doctor_id, slot.scheduled_at) not in previous for slot in result.options
            )


async def test_other_day_during_slot_offer_stays_in_current_booking():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    writes_before = runtime.emulator.write_count
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:3], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])

        result = await advance(client, runtime.call_id, 4, "А можно на другой день?")
        state = await runtime.dialogue_store.inspect(runtime.call_id)

        assert result.code == "OTHER_DAY_REQUESTED"
        assert result.phase == "offered" and state.purpose == "booking"
        assert state.specialty == "терапевт" and state.options
        assert runtime.emulator.write_count == writes_before


async def test_other_day_at_booking_confirmation_releases_hold_without_reschedule():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    writes_before = runtime.emulator.write_count
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:4], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])
        state = await runtime.dialogue_store.inspect(runtime.call_id)
        action = await runtime.booking_store.get(state.proposal.action_id)

        result = await advance(client, runtime.call_id, 5, "А можно на другой день?")
        updated = await runtime.dialogue_store.inspect(runtime.call_id)

        assert result.code == "OTHER_DAY_REQUESTED"
        assert updated.phase == "identified" and updated.proposal is None
        assert (await runtime.booking_store.get(action.action_id)).state.value == "cancelled"
        assert runtime.emulator.write_count == writes_before


async def test_repeat_replays_last_active_question_without_changing_phase():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    async with runtime.application() as client:
        first = await advance(client, runtime.call_id, 1, patient_steps(dialog)[0]["patient"])
        state = await runtime.dialogue_store.inspect(runtime.call_id)

        repeated = await advance(client, runtime.call_id, 2, "Повторите, пожалуйста.")
        after = await runtime.dialogue_store.inspect(runtime.call_id)

        assert first.utterances[-1] == repeated.utterances[0] == state.last_question
        assert repeated.code == "QUESTION_REPEATED"
        assert after.phase == state.phase


async def test_repeat_replays_reschedule_source_question():
    dialog = dialog_by_label("M-03")
    runtime = ScenarioRuntime(dialog)
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:2], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])
        state = await runtime.dialogue_store.inspect(runtime.call_id)
        assert state.phase == "move_source"

        repeated = await advance(client, runtime.call_id, 3, "Повторите, пожалуйста.")
        after = await runtime.dialogue_store.inspect(runtime.call_id)

        assert repeated.code == "QUESTION_REPEATED"
        assert repeated.utterances == (state.last_question,)
        assert after.phase == state.phase


async def test_price_aside_returns_to_the_same_booking_question():
    dialog = dialog_by_label("M-01")
    runtime = ScenarioRuntime(dialog)
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:3], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])
        before = await runtime.dialogue_store.inspect(runtime.call_id)

        result = await advance(client, runtime.call_id, 4, "Сколько стоит приём?")
        after = await runtime.dialogue_store.inspect(runtime.call_id)

        assert result.code == "PRICE_UNAVAILABLE"
        assert "уточнит регистратор" in " ".join(result.utterances).casefold()
        assert after.phase == before.phase == "offered"
        assert after.specialty == before.specialty and after.options == before.options


async def test_visit_detour_preserves_booking_and_resumes_only_on_yes():
    dialog = dialog_by_label("M-01")
    dialog.setup = {
        **dialog.setup,
        "visits": [{"client": "smirnova_ov", "doctor": "kuznetsova", "at": "06.10.2026 10:00"}],
    }
    runtime = ScenarioRuntime(dialog)
    writes_before = runtime.emulator.write_count
    async with runtime.application() as client:
        for turn_id, step in enumerate(patient_steps(dialog)[:3], start=1):
            await advance(client, runtime.call_id, turn_id, step["patient"])
        original = await runtime.dialogue_store.inspect(runtime.call_id)
        assert original.phase == "identified" and original.specialty == "терапевт"

        detour = await advance(client, runtime.call_id, 4, "Хочу проверить свои приёмы")
        state = await runtime.dialogue_store.inspect(runtime.call_id)
        assert detour.code == "INFO_GIVEN" and detour.phase == "visits_shown"
        assert detour.question.key == "return_to_booking"
        assert state.suspended_booking.specialty == original.specialty
        assert state.purpose == "visits" and state.client_id == original.client_id
        assert runtime.emulator.write_count == writes_before

        resumed = await advance(client, runtime.call_id, 5, "Да")
        state = await runtime.dialogue_store.inspect(runtime.call_id)
        assert resumed.phase == "identified" and state.specialty == original.specialty
        assert resumed.code == "BOOKING_RESUMED"
        assert "какой день" in " ".join(resumed.utterances).casefold()
        assert state.purpose == "booking" and state.suspended_booking is None
        assert runtime.emulator.write_count == writes_before
