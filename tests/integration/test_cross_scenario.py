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
            assert all((slot.doctor_id, slot.scheduled_at) not in previous for slot in result.options)
