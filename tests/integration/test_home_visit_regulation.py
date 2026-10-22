from datetime import date

import pytest
from fixture_bridge import pipeline, turn

from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.service import HandoffService, HandoffSettings


@pytest.mark.parametrize(
    ("now", "requested"),
    [
        ("22.10.2026 13:59", date(2026, 10, 22)),
        ("22.10.2026 14:00", date(2026, 10, 23)),
        ("24.10.2026 11:59", date(2026, 10, 24)),
        ("24.10.2026 12:00", date(2026, 10, 26)),
    ],
)
async def test_home_visit_cutoffs_use_today_or_next_intake_day(now, requested):
    async with pipeline({"now": now}) as (client, service, *_):
        result = await turn(client, 1, "Мне нужно вызвать врача на дом")
        assert result["code"] == "HOME_VISIT_CONTINUE"
        state = await service.store.inspect("scenario")
        assert state.home_visit_date.date() == requested
        assert state.home_visit_stage == "confirm"


@pytest.mark.parametrize("now", ["25.10.2026 10:00", "04.11.2026 10:00"])
async def test_home_visits_are_not_accepted_on_sundays_or_holidays(now):
    async with pipeline({"now": now}) as (client, service, *_):
        result = await turn(client, 1, "Как вызвать врача на дом?")
        assert result["code"] == "HOME_VISIT_CLOSED"
        assert (await service.store.inspect("scenario")).home_visit_stage is None


async def test_home_visit_intake_before_opening_reports_the_correct_hours():
    async with pipeline({"now": "22.10.2026 07:00"}) as (client, service, *_):
        result = await turn(client, 1, "Вызов врача на дом нужен")
        assert result["code"] == "HOME_VISIT_INTAKE_CLOSED"
        assert "семи тридцати до четырнадцати" in " ".join(result["utterances"])
        assert (await service.store.inspect("scenario")).home_visit_stage is None


async def test_under_one_year_home_visit_uses_r11_p1_instead_of_the_regular_queue():
    async with pipeline({"now": "16.03.2020 10:00"}) as (client, service, *_):
        await turn(client, 1, "Мне нужен вызов врача на дом")
        await turn(client, 2, "Да")
        result = await turn(client, 3, "Смирнова Алиса Дмитриевна, 7 апреля 2019 года")
        assert result["transfer"]["route"] == "R-11"
        assert result["transfer"]["priority"] == "P1"
        assert result["phase"] == "handoff"
        assert (await service.store.inspect("scenario")).home_visit_request_id is None


async def test_home_visit_uses_exact_arrival_phrase_and_operator_card():
    async with pipeline({"now": "22.10.2026 13:00"}) as (client, service, emulator, clock, *_):
        store = MemoryHandoffStore()
        service.handoffs = HandoffService(
            store,
            service.booking.mis,
            clock,
            b"x" * 32,
            HandoffSettings(enabled=True),
        )
        await turn(client, 1, "Мне нужно вызвать врача на дом")
        await turn(client, 2, "Да")
        await turn(client, 3, "Смирнова Ольга Викторовна, 12 марта 1978 года")
        await turn(client, 4, "Улица Лесная, дом двенадцать, квартира сорок")
        await turn(client, 5, "Температура и сильная слабость")
        accepted = await turn(client, 6, "Нет, сама прийти не могу")

        assert accepted["code"] == "HOME_VISIT_REQUESTED"
        assert (
            "Врач придёт до восемнадцати часов, точное время назвать не могу."
            in accepted["utterances"]
        )
        request = await store.request_for_call("scenario")
        payload = service.handoffs.decrypt_home_visit_payload(request)
        assert payload["address"].startswith("Улица Лесная")
        assert payload["requested_date"] == "2026-10-22"
        assert "подтвердить дату и адрес" in request.summary
        assert emulator.write_count == emulator.patch_count == 0
