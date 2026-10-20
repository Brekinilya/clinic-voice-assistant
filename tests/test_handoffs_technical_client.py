"""Технический клиент для экстренных заявок T08: POST /clients без дублей и команда настройки."""

import httpx
import pytest

from app.demo import DemoClock, demo_emulator
from app.handoffs.cli import TECHNICAL, parser, technical_client
from app.mis.http import HttpMedicalSystem
from app.mis.models import ClientCreate
from app.mis.ports import MisOutcomeUnknown
from tests.test_mis import BudgetSpy


def stand(emulator=None):
    emulator = emulator or demo_emulator(DemoClock())
    client = httpx.AsyncClient(
        base_url="https://mis.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    )
    return emulator, client


async def test_repeated_registration_returns_the_same_card():
    emulator, client = stand()
    async with client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        first = await mis.create_client(TECHNICAL)
        again = await mis.create_client(TECHNICAL)
    assert first.id == again.id and first.id not in (1, 2, 3)
    assert (first.middle_name, first.birth_date.year) == ("Технический", 1900)
    assert emulator.write_count == 1
    assert len(emulator.clients["demo"]) == 4


async def test_check_finds_nothing_until_the_card_is_created_and_then_suggests_the_setting():
    _, client = stand()
    async with client:
        mis = HttpMedicalSystem(client, BudgetSpy())
        before = await technical_client(mis, check=True, configured=None)
        created = await technical_client(mis, check=False, configured=None)
        after = await technical_client(mis, check=True, configured=None)
        settled = await technical_client(mis, check=True, configured=created["technical_client_id"])
    assert before == {"technical_client_id": None, "matches": 0, "configured": None}
    client_id = created["technical_client_id"]
    assert created["next"] == f"HANDOFF_TECHNICAL_CLIENT_ID={client_id} в .env"
    assert after["technical_client_id"] == client_id and after["matches"] == 1
    assert "next" not in settled


async def test_foreign_card_in_the_answer_is_an_unknown_outcome():
    # Стенд ответил карточкой другого человека: это не «клиент заведён»
    def handle(request):
        return httpx.Response(
            201,
            json={
                "id": 9,
                "last_name": "Другой",
                "first_name": "Человек",
                "middle_name": None,
                "birth_date": "01.01.1900",
                "comment": None,
                "created_at": "19.10.2026 10:00",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://mis.invalid/api/v1/", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(MisOutcomeUnknown):
            await HttpMedicalSystem(client, BudgetSpy()).create_client(TECHNICAL)


def test_technical_card_is_plainly_fictional_and_cli_has_a_dry_check():
    assert isinstance(TECHNICAL, ClientCreate) and TECHNICAL.birth_date.year == 1900
    args = parser().parse_args(["technical-client", "--check"])
    assert (args.command, args.check) == ("technical-client", True)
