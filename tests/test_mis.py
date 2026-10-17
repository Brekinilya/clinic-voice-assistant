"""Проверяем формат реального API независимо от реализации эмулятора."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from app.demo import DemoClock, demo_emulator
from app.mis.budget import MemoryBudget, Priority, demand
from app.mis.http import HttpMedicalSystem
from app.mis.models import Visit, VisitChange, VisitCreate, VisitStatus, parse_date, parse_datetime
from app.mis.ports import MisError, MisOutcomeUnknown, MisUnavailable
from app.settings import Settings

DOCTOR = {
    "id": 3,
    "full_name": "Примерова Анна Тестовна",
    "specialty": "терапевт",
    "is_active": True,
}
VISIT = {
    "id": 7,
    "client_id": 1,
    "doctor": DOCTOR,
    "scheduled_at": "15.10.2026 10:00",
    "status": "Запланирована",
    "created_at": "14.10.2026 09:00",
    "updated_at": "14.10.2026 09:00",
}


class BudgetSpy:
    def __init__(self):
        self.reservations = 0
        self.delays = []

    async def reserve(self, cost=1):
        self.reservations += cost

    async def release(self, ids):
        pass

    async def defer(self, seconds):
        self.delays.append(seconds)


@pytest.mark.parametrize("value", ["2026-10-14", "31.02.2026", "1.10.2026", "14.10.2026 10:00"])
def test_invalid_dates_are_not_guessed(value):
    with pytest.raises(ValueError):
        parse_date(value)


def test_timezone_and_minute_precision():
    data = VisitCreate(
        client_id=1, doctor_id=3, scheduled_at=datetime(2026, 10, 15, 6, tzinfo=timezone.utc)
    )
    assert data.model_dump(mode="json")["scheduled_at"] == "15.10.2026 10:00"
    for value in ("15.10.2026 10:00:01", "15.10.2026T10:00+04:00", datetime(2026, 10, 15, 10)):
        with pytest.raises(ValueError):
            parse_datetime(value)
    with pytest.raises(ValidationError):
        VisitCreate(client_id=True, doctor_id="3", scheduled_at="15.10.2026 10:00")
    with pytest.raises(ValidationError):
        VisitCreate(client_id=1, doctor_id=3, scheduled_at="15.10.2026 10:00", typo="ignored")


async def test_create_wire_format_and_success():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(201, json=VISIT)

    budget = BudgetSpy()
    async with httpx.AsyncClient(
        base_url="https://mis.invalid/api/v1/", transport=httpx.MockTransport(handle)
    ) as client:
        result = await HttpMedicalSystem(client, budget).create_visit(
            VisitCreate(client_id=1, doctor_id=3, scheduled_at="15.10.2026 10:00")
        )
    assert result.id == 7
    assert result.scheduled_at.hour == 10
    assert result.scheduled_at.utcoffset() == timedelta(hours=4)
    assert requests[0].url.path == "/api/v1/visits"
    assert requests[0].content == b'{"client_id":1,"doctor_id":3,"scheduled_at":"15.10.2026 10:00"}'
    assert budget.reservations == 1


async def test_pagination_completes_all_pages():
    offsets = []

    def handle(request):
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200, json=[{**DOCTOR, "id": n} for n in range(offset + 1, min(offset + 51, 53))]
        )

    budget = BudgetSpy()
    async with httpx.AsyncClient(
        base_url="https://mis.invalid/", transport=httpx.MockTransport(handle)
    ) as client:
        result = await HttpMedicalSystem(client, budget).doctors("терапевт")
    assert len(result) == 52 and offsets == [0, 50] and budget.reservations == 2


async def test_repeating_page_cannot_be_reported_as_complete():
    def handle(request):
        return httpx.Response(200, json=[{**DOCTOR, "id": i} for i in range(1, 51)])

    async with httpx.AsyncClient(
        base_url="https://mis.invalid/", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(MisUnavailable, match="INVALID_RESPONSE"):
            await HttpMedicalSystem(client, BudgetSpy()).doctors()


@pytest.mark.parametrize(
    "status,exception",
    [
        (400, MisError),
        (401, MisError),
        (404, MisError),
        (409, MisError),
        (500, MisOutcomeUnknown),
        (302, MisOutcomeUnknown),
    ],
)
async def test_write_errors_never_retry_or_expose_body(status, exception):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"message": "private-token-and-patient"},
            headers={"Location": "https://elsewhere.invalid"},
        )

    async with httpx.AsyncClient(
        base_url="https://mis.invalid/", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(exception) as error:
            await HttpMedicalSystem(client, BudgetSpy()).create_visit(
                VisitCreate(client_id=1, doctor_id=3, scheduled_at="15.10.2026 10:00")
            )
    assert len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("write", [False, True])
async def test_transport_failure_classification(write):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-token", request=request)

    async with httpx.AsyncClient(
        base_url="https://mis.invalid/", transport=httpx.MockTransport(handle)
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        with pytest.raises(MisOutcomeUnknown if write else MisUnavailable) as error:
            if write:
                await mis.create_visit(
                    VisitCreate(client_id=1, doctor_id=3, scheduled_at="15.10.2026 10:00")
                )
            else:
                await mis.doctors()
    assert len(calls) == 1 and "private" not in str(error.value)


async def test_invalid_successful_write_requires_reconciliation():
    async with httpx.AsyncClient(
        base_url="https://mis.invalid/",
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={})),
    ) as client:
        with pytest.raises(MisOutcomeUnknown, match="INVALID_RESPONSE"):
            await HttpMedicalSystem(client, BudgetSpy()).create_visit(
                VisitCreate(client_id=1, doctor_id=3, scheduled_at="15.10.2026 10:00")
            )


async def test_retry_after_is_shared_and_not_slept_or_retried():
    budget = BudgetSpy()
    async with httpx.AsyncClient(
        base_url="https://mis.invalid/",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "17"})),
    ) as client:
        with pytest.raises(MisError) as error:
            await HttpMedicalSystem(client, budget).doctors()
    assert error.value.retry_after == 17 and budget.delays == [17] and budget.reservations == 1


async def test_local_budget_concurrency_and_window_boundary():
    clock = DemoClock()
    budget = MemoryBudget(clock)
    # Всё окно доступно только записи; обычный разговор оставляет ей одно место.
    with demand(Priority.COMMIT):
        results = await asyncio.gather(
            *(budget.reserve() for _ in range(12)), return_exceptions=True
        )
    assert sum(isinstance(r, list) for r in results) == 8
    assert all(r.retry_after == 60 for r in results if isinstance(r, MisError))
    clock.value += timedelta(seconds=60)
    await budget.reserve()
    await budget.defer(120)
    clock.value += timedelta(seconds=60)
    with pytest.raises(MisError) as error:
        await budget.reserve()
    assert error.value.retry_after == 60


def test_live_client_cannot_be_created_in_offline_mode():
    with pytest.raises(ValueError):
        HttpMedicalSystem.live_client(Settings(_env_file=None))


async def test_cache_is_bounded_by_time_and_token_and_fresh_bypasses_it():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    elapsed = [0.0]
    budget = BudgetSpy()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, budget, elapsed=lambda: elapsed[0])
        patient = (await mis.clients("Тестов", "Александр", parse_date("01.01.1990")))[0]
        assert await mis.client(patient.id) == patient
        assert budget.reservations == 1
        await mis.doctors("терапевт")
        emulator.doctors[1] = emulator.doctors[1].model_copy(update={"is_active": False})
        assert (await mis.doctors("терапевт"))[0].is_active
        assert not (await mis.doctors("терапевт", fresh=True))[0].is_active
        elapsed[0] = 301
        await mis.client(patient.id)
        assert budget.reservations == 4
        client.headers["Authorization"] = "Bearer unknown"
        with pytest.raises(MisError, match="UNAUTHORIZED"):
            await mis.client(patient.id)


@pytest.mark.parametrize("refresh", ["fresh", "other_call", "end", "expiry", "unscoped", "range"])
async def test_visit_snapshot_is_scoped_and_only_covers_requested_dates(refresh):
    clock = DemoClock()
    elapsed = [0.0]
    emulator = demo_emulator(clock)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy(), elapsed=lambda: elapsed[0])
        params = {
            "doctor_id": 1,
            "status": VisitStatus.PLANNED,
            "date_from": parse_date("15.10.2026"),
            "date_to": parse_date("18.10.2026"),
            "call_id": "a",
        }
        assert await mis.visits(**params) == []
        visit = Visit.model_validate({**VISIT, "doctor": emulator.doctors[1], "client_id": 1})
        emulator.records["demo"][visit.id] = visit
        # Выбор внутри покрытого периода использует снимок этого звонка.
        assert await mis.visits(**{**params, "date_to": params["date_from"]}) == []
        if refresh == "fresh":
            params["fresh"] = True
        elif refresh == "other_call":
            params["call_id"] = "b"
        elif refresh == "end":
            mis.clear_call_cache("a")
        elif refresh == "expiry":
            elapsed[0] = 90
        elif refresh == "unscoped":
            del params["call_id"]
        else:
            params["date_from"] = parse_date("14.10.2026")
        assert await mis.visits(**params) == [visit]


async def test_visit_snapshot_does_not_cross_token_or_patient_query():
    clock = DemoClock()
    emulator = demo_emulator(clock)
    visit = Visit.model_validate({**VISIT, "doctor": emulator.doctors[1], "client_id": 1})
    emulator.records["demo"][visit.id] = visit
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        assert await mis.visits(client_id=1, call_id="a") == [visit]
        assert await mis.visits(client_id=2, call_id="a") == []
        client.headers["Authorization"] = "Bearer unknown"
        with pytest.raises(MisError, match="UNAUTHORIZED"):
            await mis.visits(client_id=1, call_id="a")


@pytest.mark.parametrize("failure", [None, "lost_response"])
async def test_visit_write_invalidates_other_calls_even_after_lost_response(failure):
    emulator = demo_emulator(DemoClock())
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        assert await mis.visits(doctor_id=1, call_id="other") == []
        emulator.next_failure = failure
        request = VisitCreate(client_id=1, doctor_id=1, scheduled_at="15.10.2026 10:00")
        if failure:
            with pytest.raises(MisOutcomeUnknown):
                await mis.create_visit(request)
        else:
            await mis.create_visit(request)
        assert len(await mis.visits(doctor_id=1, call_id="other")) == 1
        assert emulator.write_count == 1


async def test_emulator_keeps_duplicates_and_supports_cancel_semantics():
    emulator = demo_emulator(DemoClock())
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        data = VisitCreate(client_id=1, doctor_id=1, scheduled_at="15.10.2026 10:00")
        a, b = await mis.create_visit(data), await mis.create_visit(data)
        assert a.id != b.id and emulator.write_count == 2
        cancelled = await mis.change_visit(a.id, VisitChange(status=VisitStatus.CANCELLED))
        assert (
            await mis.change_visit(a.id, VisitChange(status=VisitStatus.CANCELLED))
        ) == cancelled
        with pytest.raises(MisError, match="CONFLICT"):
            await mis.change_visit(a.id, VisitChange(scheduled_at="16.10.2026 10:00"))
        assert len(await mis.visits(client_id=1, status=VisitStatus.PLANNED)) == 1
        with pytest.raises(ValueError):
            await mis.change_visit(b.id, VisitChange(status=None))


async def test_emulator_isolates_teams_and_uses_inclusive_date_filters():
    emulator = demo_emulator(DemoClock())
    emulator.clients["other"] = dict(emulator.clients["demo"])
    emulator.records["other"] = {}
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        await mis.create_visit(
            VisitCreate(client_id=1, doctor_id=1, scheduled_at="15.10.2026 23:59")
        )
        assert (
            len(
                await mis.visits(
                    date_from=parse_date("15.10.2026"), date_to=parse_date("15.10.2026")
                )
            )
            == 1
        )
        client.headers["Authorization"] = "Bearer other"
        assert await mis.visits() == []
        assert len(await mis.doctors()) == 1
        client.headers["Authorization"] = "Bearer unknown"
        with pytest.raises(MisError, match="UNAUTHORIZED"):
            await mis.visits()
