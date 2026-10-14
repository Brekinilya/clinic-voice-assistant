"""Адаптация фикстур Ильи к HTTP-эмулятору; допуски здесь явно учебные."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml

from app.booking.memory import MemoryBookingStore
from app.booking.models import Slot
from app.booking.service import BookingService
from app.cancellation.memory import MemoryCancellationStore
from app.cancellation.service import CancellationService
from app.demo import EmulatorBudget
from app.dialogue.memory import MemoryDialogueStore
from app.dialogue.natural import BookingConversation
from app.dialogue.safety_adapter import RulesSafety
from app.dialogue.service import DialogueService
from app.main import create_app
from app.mis.emulator import MisEmulator
from app.mis.http import HttpMedicalSystem
from app.mis.models import Client, Doctor, Visit, VisitStatus, parse_datetime
from app.settings import Settings

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
KEY = b"fictional-stable-key-for-integration-at-least-32"


class ScenarioClock:
    def __init__(self, value="30.09.2026 10:15"):
        self.value = parse_datetime(value)

    def now(self):
        return self.value


class FixtureSchedule:
    def __init__(self, slots, allowed):
        self._slots, self.allowed = tuple(slots), allowed

    def slots(self):
        return self._slots

    def eligible(self, client_id, slot):
        return (client_id, slot.doctor_id) in self.allowed


@asynccontextmanager
async def pipeline(setup=None, *, pg_engine=None):
    setup = setup or {}
    clock = ScenarioClock(setup.get("now", "30.09.2026 10:15"))
    raw = yaml.safe_load((SCENARIOS / "fixtures.yaml").read_text(encoding="utf-8"))
    doctor_ids = {name: i for i, name in enumerate(raw["doctors"], 1)}
    client_ids = {name: i for i, name in enumerate(raw["clients"], 1)}
    doctors = [
        Doctor(id=doctor_ids[key], **{k: v for k, v in data.items() if k != "room"})
        for key, data in raw["doctors"].items()
    ]
    clients = [
        Client(id=client_ids[key], **data, comment=None, created_at=clock.now())
        for key, data in raw["clients"].items()
    ]
    slots = [
        Slot(
            doctor_id=doctor_ids[key],
            specialty=raw["doctors"][key]["specialty"],
            scheduled_at=parse_datetime(at),
        )
        for key, values in raw["slots"].items()
        for at in values
    ]
    # Для выбранных сценариев допуск — явный факт учебного окружения, не свойство настоящей МИС.
    allowed = {
        (client_ids["smirnova_ov"], doctor_ids["kuznetsova"]),
        (client_ids["ivanov_sp"], doctor_ids["belova"]),
    }
    emulator = MisEmulator(clock, doctors, {"demo": clients})
    for i, data in enumerate(setup.get("visits", []), 1):
        emulator.records["demo"][i] = Visit(
            id=i,
            client_id=client_ids[data["client"]],
            doctor=emulator.doctors[doctor_ids[data["doctor"]]],
            scheduled_at=parse_datetime(data["at"]),
            status=VisitStatus(data.get("status", VisitStatus.PLANNED)),
            created_at=clock.now(),
            updated_at=clock.now(),
        )

    class FixtureRooms:
        def room(self, doctor_id, scheduled_at):
            # В учебном справочнике кабинеты постоянные; реальный источник может учитывать дату.
            key = next(key for key, value in doctor_ids.items() if value == doctor_id)
            value = raw["doctors"][key].get("room")
            return None if value is None else str(value)

    if pg_engine is None:
        dialogue_store = MemoryDialogueStore()
        booking_store = MemoryBookingStore(guard_reader=dialogue_store)
        cancellation_store = MemoryCancellationStore(booking_store.ledger, dialogue_store)
    else:
        from app.storage.booking import PostgresBookingStore
        from app.storage.cancellation import PostgresCancellationStore
        from app.storage.dialogue import PostgresDialogueStore

        dialogue_store = PostgresDialogueStore(pg_engine)
        booking_store = PostgresBookingStore(pg_engine)
        cancellation_store = PostgresCancellationStore(pg_engine)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as mis_client:
        booking = BookingService(
            HttpMedicalSystem(mis_client, EmulatorBudget()),
            booking_store,
            FixtureSchedule(slots, allowed),
            clock,
            require_dialogue_guard=True,
        )
        safety = RulesSafety(clock)
        conversation = BookingConversation(
            booking,
            dialogue_store,
            safety,
            rooms=FixtureRooms(),
            cancellation=CancellationService(booking.mis, cancellation_store, clock),
        )
        service = DialogueService(
            booking, dialogue_store, KEY, safety=safety, conversation=conversation
        )

        @asynccontextmanager
        async def factory(settings, selected_clock):
            yield service

        app = create_app(
            Settings(_env_file=None, backend_demo_enabled=True, dialogue_mode="natural"),
            clock=clock,
            dialogue_factory=factory,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                yield client, service, emulator, clock, doctor_ids, client_ids


async def turn(client, number, text, *, call_id="scenario", final=True):
    response = await client.post(
        f"/api/dialog/{call_id}/turn", json={"turn_id": number, "text": text, "is_final": final}
    )
    assert response.status_code == 200, response.text
    return response.json()
