"""Недостающий допуск не маскируется отсутствием слотов и не обходит ограничения."""

from fixture_bridge import pipeline, turn

from app.booking.schedule import AccessState


async def test_missing_assignment_is_explained_and_specialty_can_be_changed():
    async with pipeline() as (client, service, emulator, clock, *_):
        schedule = service.booking.schedule
        schedule.allowed.clear()
        schedule.access_state = lambda client_id, slot: AccessState.UNKNOWN
        await turn(client, 1, "Хочу записаться к терапевту")
        result = await turn(client, 2, "Смирнова Ольга Викторовна, 12.03.1978")
        assert result["code"] == "ELIGIBILITY_UNKNOWN"
        assert result["phase"] == "identified" and not result["options"]
        assert "регистратора" in " ".join(result["utterances"])
        assert "слотов нет" not in " ".join(result["utterances"])
        operator = await turn(client, 3, "Позовите оператора")
        assert operator["phase"] == "handoff"
        assert emulator.write_count == 0
