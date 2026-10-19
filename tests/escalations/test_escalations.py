"""T08-a: заявки /escalations без дублей, сверка чтением, технический клиент."""

import asyncio
from datetime import date, datetime, timedelta

import httpx
import pytest

from app.clock import CLINIC_TIMEZONE
from app.demo import demo_emulator
from app.escalations.memory import MemoryEscalationStore
from app.escalations.models import CHECK_AFTER, EscalationRecord, compose, digest
from app.escalations.service import EscalationService
from app.mis.http import HttpMedicalSystem
from app.mis.models import ESCALATION_LIMIT, ClientCreate, EscalationCreate
from app.mis.ports import MisOutcomeUnknown
from app.storage.escalations import PostgresEscalationStore

CARD = "R-03 жалоба, P2. Личность подтверждена. Пациент недоволен ожиданием."


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 20, 11, tzinfo=CLINIC_TIMEZONE)

    def now(self):
        return self.value


class FreeBudget:
    async def reserve(self, cost=1):
        return None

    async def release(self, ids):
        pass

    async def touch(self, place):
        return 0.0

    async def defer(self, seconds):
        pass


@pytest.fixture(params=["memory", "postgres"])
def escalation_store(request):
    if request.param == "memory":
        return MemoryEscalationStore()
    return PostgresEscalationStore(request.getfixturevalue("pg_engine"))


class Stand:
    def __init__(self, store, technical=None):
        self.clock = Clock()
        self.emulator = demo_emulator(self.clock)
        self.http = httpx.AsyncClient(
            base_url="https://offline.invalid/api/v1/",
            headers={"Authorization": "Bearer demo"},
            transport=self.emulator.transport(),
        )
        self.mis = HttpMedicalSystem(self.http, FreeBudget())
        self.store = store
        self.service = EscalationService(self.mis, store, self.clock, technical_client_id=technical)

    def created(self):
        return list(self.emulator.escalations["demo"].values())


@pytest.fixture
async def stand(escalation_store):
    value = Stand(escalation_store)
    yield value
    await value.http.aclose()


async def test_one_call_and_route_is_one_request_even_when_repeated(stand):
    first = await stand.service.request("call-1", "R-03", "P2", CARD, client_id=3)
    again = await stand.service.request("call-1", "R-03", "P2", CARD, client_id=3)
    assert first.mis_status == again.mis_status == "applied"
    assert first.escalation_id == again.escalation_id
    [item] = stand.created()
    assert item.client_id == 3 and item.description.startswith(CARD)
    assert "Заявка ассистента №" in item.description
    other = await stand.service.request("call-1", "R-05", "P3", "Не понимаем", client_id=3)
    assert other.escalation_id != first.escalation_id and len(stand.created()) == 2


async def test_concurrent_requests_for_the_same_route_post_once(stand):
    results = await asyncio.gather(
        *(stand.service.request("call-1", "R-01", "P1", CARD, client_id=3) for _ in range(5))
    )
    assert len(stand.created()) == 1
    assert {r.ref for r in results} == {"call-1:R-01"}


async def test_lost_response_is_settled_by_reading_the_list(stand):
    stand.emulator.next_failure = "lost_response"
    status = await stand.service.request("call-1", "R-03", "P2", CARD, client_id=3)
    assert status.mis_status == "pending"
    assert await stand.service.reconcile() == 0  # срок сверки ещё не наступил
    stand.clock.value += CHECK_AFTER
    assert await stand.service.reconcile() == 1
    status = await stand.service.status("call-1", "R-03")
    assert status.mis_status == "applied" and status.escalation_id == stand.created()[0].id
    assert len(stand.created()) == 1


async def test_unknown_request_is_never_posted_again(stand):
    sent = []

    async def lost(request):
        sent.append(request)
        raise MisOutcomeUnknown("TRANSPORT_FAILURE")

    stand.mis.create_escalation = lost
    assert (await stand.service.request("call-1", "R-03", "P2", CARD, 3)).mis_status == "pending"
    for _ in range(3):
        stand.clock.value += CHECK_AFTER
        await stand.service.reconcile()
    status = await stand.service.request("call-1", "R-03", "P2", CARD, 3)
    assert (status.mis_status, status.reason) == ("manual", "not_visible")
    assert len(sent) == 1


async def test_definite_rejection_is_manual_without_retry(stand):
    status = await stand.service.request("call-1", "R-03", "P2", CARD, client_id=999)
    assert (status.mis_status, status.reason) == ("manual", "INVALID_REQUEST")
    assert stand.created() == []


async def test_unidentified_emergency_goes_to_the_prepared_technical_client(escalation_store):
    stand = Stand(escalation_store, technical=3)
    try:
        status = await stand.service.request("call-1", "R-01", "P1", "Боль в груди", None)
        assert status.mis_status == "applied" and status.technical
        assert stand.created()[0].client_id == 3
    finally:
        await stand.http.aclose()


async def test_without_technical_client_the_request_is_manual_and_not_sent(stand):
    status = await stand.service.request("call-1", "R-01", "P1", "Боль в груди", None)
    assert (status.mis_status, status.reason) == ("manual", "no_technical_client")
    assert stand.created() == []


async def test_attempt_that_crashed_before_post_is_sent_once_with_its_marker(stand):
    now = stand.clock.now()
    text = compose(CARD, "№0011223344")
    await stand.store.prepare(
        EscalationRecord(
            id="call-1:R-03",
            call_id="call-1",
            route="R-03",
            priority="P2",
            client_id=3,
            marker="№0011223344",
            length=len(text),
            digest=digest(text),
            created_at=now,
            updated_at=now,
        )
    )
    status = await stand.service.request("call-1", "R-03", "P2", CARD, client_id=3)
    assert status.mis_status == "applied"
    [item] = stand.created()
    assert item.description.endswith("№0011223344")


def test_card_is_cut_to_the_limit_and_cleaned_but_the_marker_stays():
    text = compose("А" * 2000 + "\x07", "№abcdef0123")
    assert len(text) == ESCALATION_LIMIT and text.endswith("Заявка ассистента №abcdef0123")
    assert "…" in text and "\x07" not in text
    assert compose("  \x01 ", "№1").startswith("Причина не указана")
    EscalationCreate(client_id=1, description=compose("a\x0bb", "№1")).body()


async def test_http_adapter_lists_newest_first_and_reads_one(stand):
    for route in ("R-03", "R-05", "R-07"):
        await stand.service.request("call-1", route, "P3", CARD, client_id=3)
        stand.clock.value += timedelta(minutes=1)
    items = await stand.mis.escalations(client_id=3)
    assert [i.description.split("\n")[0] for i in items] == [CARD] * 3
    assert items[0].id > items[-1].id
    assert (await stand.mis.escalation(items[0].id)).id == items[0].id


async def test_technical_client_registration_is_idempotent(stand):
    from app.escalations.cli import TECHNICAL

    first = await stand.mis.create_client(TECHNICAL)
    second = await stand.mis.create_client(TECHNICAL)
    assert first.id == second.id and first.birth_date == date(1900, 1, 1)
    other = ClientCreate(last_name="Срочный", first_name="Вызов", birth_date=date(1900, 1, 2))
    assert (await stand.mis.create_client(other)).id != first.id
