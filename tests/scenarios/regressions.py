"""Регрессия пользовательского диалога, с вымышленными идентификаторами."""

from tests.scenarios.runtime import ScenarioRuntime
from tests.scenarios.scenario_plan import Dialog

REGRESSION_LABEL = "REG-T05 неизвестный пациент → исправление → пятница"


def quota_regression():
    dialog = Dialog(
        "REG-T05",
        "неизвестный пациент → исправление → пятница",
        {
            "now": "17.10.2026 10:15",
            "channel": "inbound",
            # Второй потребитель той же квоты заранее читает две страницы каталога.
            # Это одна память теста, не доказательство работы PostgreSQL между процессами.
            "background_catalog": True,
            "slots": {"volkov": ["19.10.2026 08:00", "19.10.2026 08:20", "23.10.2026 10:00"]},
        },
        [
            {"patient": "Хочу записаться к хирургу", "say": "ident.ask"},
            {"patient": "Смирнова Ольга Викторовна, 12.03.1980", "say": "ident.not_found"},
            {"patient": "Иванов Сергей Петрович, 03.01.1990", "say": "booking.offer_first"},
            # Пауза дольше TTL каталога воспроизводит зависимость разбора от GET doctors.
            {
                "patient": "Давайте в пятницу",
                "advance_s": 35,
                "slot_weekdays": [5],
                "say": "booking.offer_period",
            },
        ],
        {"stand": "unchanged"},
    )
    return dialog, ScenarioRuntime(dialog, internal_limit=8, external_limit=10, doctor_count=85)
