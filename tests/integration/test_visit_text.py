"""Произношение не должно потерять часовой пояс, год или ведущий ноль минут."""

from datetime import datetime, timedelta, timezone

import pytest

from app.dialogue.visit_text import appointment_text, room_text


def test_clinic_midnight_moves_to_next_day_and_weekday():
    moment = datetime(2026, 10, 4, 23, 30, tzinfo=timezone(timedelta(hours=3)))
    assert appointment_text(moment) == (
        "Приём в понедельник, пятого октября, в ноль тридцать.",
        "Год приёма: две тысячи двадцать шесть.",
    )


def test_next_year_and_five_minutes_are_announced_without_ambiguity():
    moment = datetime(2027, 1, 1, 9, 5, tzinfo=timezone(timedelta(hours=4)))
    phrases = appointment_text(moment)
    assert phrases == (
        "Приём в пятницу, первого января, в девять ноль пять.",
        "Год приёма: две тысячи двадцать семь.",
    )
    assert all(len(p.split()) <= 20 for p in phrases)
    assert room_text("118") == "Кабинет сто восемнадцать."
    assert room_text("118А") == "Кабинет 118А."


def test_timezone_is_required_for_spoken_appointment():
    with pytest.raises(ValueError, match="часовой пояс"):
        appointment_text(datetime(2026, 10, 5, 13, 20))
