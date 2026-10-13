"""Данные расписания проверяем до использования; неизвестный допуск не превращаем в разрешение."""

import json
from datetime import timedelta

import pytest

from app.booking.schedule import AccessState, JsonSchedule, ScheduleError
from app.demo import DemoClock


def document():
    return {
        "schema_version": 1,
        "timezone": "GMT+4",
        "source": "Вымышленный график для теста",
        "generated_at": "2026-10-14T08:00:00+04:00",
        "expires_at": "2026-10-20T23:59:00+04:00",
        "slots": [
            {"doctor_id": 1, "specialty": "терапевт", "scheduled_at": "2026-10-15T10:00:00+04:00"}
        ],
        "access": [
            {
                "client_id": 1,
                "doctor_id": 1,
                "state": "allowed",
                "source": "Подтверждённый учебный допуск",
                "valid_from": "2026-10-14T00:00:00+04:00",
                "valid_until": "2026-10-20T23:59:00+04:00",
            }
        ],
    }


def load(tmp_path, payload, clock=None):
    path = tmp_path / "учебное расписание.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return JsonSchedule.load(path, clock or DemoClock())


def test_explicit_slots_and_access_unknown_for_another_patient(tmp_path):
    schedule = load(tmp_path, document())
    slot = schedule.slots()[0]
    assert schedule.eligible(1, slot)
    assert schedule.access_state(2, slot) == AccessState.UNKNOWN
    assert not schedule.eligible(2, slot)
    assert slot.scheduled_at.utcoffset() == timedelta(hours=4)
    assert len(schedule.fingerprint) == 64


@pytest.mark.parametrize("state", ["denied", "unknown"])
def test_denial_and_unknown_are_both_closed(tmp_path, state):
    payload = document()
    payload["access"][0]["state"] = state
    schedule = load(tmp_path, payload)
    assert schedule.access_state(1, schedule.slots()[0]) == state
    assert not schedule.eligible(1, schedule.slots()[0])


def test_missing_and_expired_access_never_enable_booking(tmp_path):
    payload = document()
    payload["access"] = []
    schedule = load(tmp_path, payload)
    assert not schedule.eligible(1, schedule.slots()[0])
    payload["access"] = document()["access"]
    payload["access"][0]["valid_until"] = "2026-10-14T23:59:00+04:00"
    schedule = load(tmp_path, payload)
    assert not schedule.eligible(1, schedule.slots()[0])


def test_expired_snapshot_is_unavailable_even_after_initial_load(tmp_path):
    clock = DemoClock()
    schedule = load(tmp_path, document(), clock)
    slot = schedule.slots()[0]
    clock.value += timedelta(days=7)
    assert schedule.slots() == () and not schedule.eligible(1, slot)
    with pytest.raises(ScheduleError, match="SCHEDULE_NOT_CURRENT"):
        load(tmp_path, document(), clock)


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate",
        "conflicting_specialty",
        "overlap",
        "unknown_doctor",
        "naive",
        "seconds",
        "extra",
        "version",
    ],
)
def test_bad_schedule_rejected_as_a_whole(tmp_path, problem):
    payload = document()
    if problem == "duplicate":
        payload["slots"].append(dict(payload["slots"][0]))
    if problem == "conflicting_specialty":
        payload["slots"].append(
            {
                **payload["slots"][0],
                "specialty": "хирург",
                "scheduled_at": "2026-10-15T11:00:00+04:00",
            }
        )
    if problem == "overlap":
        payload["access"].append({**payload["access"][0], "state": "denied"})
    if problem == "unknown_doctor":
        payload["access"][0]["doctor_id"] = 99
    if problem == "naive":
        payload["slots"][0]["scheduled_at"] = "2026-10-15T10:00:00"
    if problem == "seconds":
        payload["slots"][0]["scheduled_at"] = "2026-10-15T10:00:01+04:00"
    if problem == "extra":
        payload["private-patient"] = "private-content"
    if problem == "version":
        payload["schema_version"] = 2
    with pytest.raises(ScheduleError, match="INVALID_SCHEDULE") as error:
        load(tmp_path, payload)
    assert "private" not in str(error.value)


def test_missing_and_oversized_file_are_safe_errors(tmp_path):
    with pytest.raises(ScheduleError, match="INVALID_SCHEDULE"):
        JsonSchedule.load(tmp_path / "отсутствует.json", DemoClock())
    large = tmp_path / "large.json"
    large.write_bytes(b" " * 4_000_001)
    with pytest.raises(ScheduleError, match="SCHEDULE_TOO_LARGE"):
        JsonSchedule.load(large, DemoClock())


def test_boolean_version_and_duplicate_json_key_are_rejected(tmp_path):
    payload = document()
    payload["schema_version"] = True
    with pytest.raises(ScheduleError, match="INVALID_SCHEDULE"):
        load(tmp_path, payload)
    path = tmp_path / "duplicate.json"
    path.write_text(
        json.dumps(document()).replace(
            '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleError, match="INVALID_SCHEDULE"):
        JsonSchedule.load(path, DemoClock())
