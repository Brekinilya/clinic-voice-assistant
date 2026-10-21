"""Сборка настоящего приложения с отдельными вымышленными ресурсами на диалог."""

import hashlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import httpx

from app.booking.memory import MemoryBookingStore
from app.booking.models import Slot
from app.booking.schedule import AccessFact, JsonSchedule, ScheduleDocument
from app.cancellation.memory import MemoryCancellationStore
from app.clock import CLINIC_TIMEZONE
from app.demo import DemoClock, EmulatorBudget
from app.dialogue.composition import assemble_dialogue
from app.dialogue.memory import MemoryDialogueStore
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.service import HandoffService, HandoffSettings
from app.main import create_app
from app.mis.budget import MemoryBudget
from app.mis.http import HttpMedicalSystem
from app.mis.models import Client, Doctor, Visit, VisitStatus
from app.reminders.calendar import ClinicCalendar
from app.reminders.dialogue_adapter import ReminderActionAdapter
from app.reminders.executor import ReminderExecutor
from app.reminders.marks import ReminderMarks
from app.reminders.memory import MemoryReminderStore
from app.reminders.models import Contact
from app.rescheduling.memory import MemoryMoveStore
from app.settings import Settings
from tests.scenarios.emulator import ScenarioEmulator
from tests.scenarios.scenario_plan import STAND_TIME, load_fixtures

TOKEN = "scenario-only-fictional-token"
REMINDER_ENDPOINT = "5550101"
REMINDER_OWNER = "scenario-runtime"


def moment(value):
    return datetime.strptime(value, STAND_TIME).replace(tzinfo=CLINIC_TIMEZONE)


class NoExternalDatabase:
    async def ping(self):
        pass

    async def aclose(self):
        pass


class ScenarioRuntime:
    def __init__(self, dialog, *, internal_limit=None, external_limit=None, doctor_count=None):
        self.dialog = dialog
        fixtures = load_fixtures()
        self.clock = DemoClock()
        self.clock.value = dialog.now.replace(tzinfo=CLINIC_TIMEZONE)
        self.doctor_ids = {key: index for index, key in enumerate(fixtures["doctors"], 1)}
        self.client_ids = {key: index for index, key in enumerate(fixtures["clients"], 1)}
        doctors = [
            Doctor(id=self.doctor_ids[key], **{k: v for k, v in item.items() if k != "room"})
            for key, item in fixtures["doctors"].items()
        ]
        if doctor_count is not None:
            if doctor_count < len(doctors):
                raise ValueError("doctor_count меньше исходного справочника")
            doctors.extend(
                Doctor(id=i, full_name=f"Врач Номер {i}", specialty="хирург", is_active=True)
                for i in range(len(doctors) + 1, doctor_count + 1)
            )
        clients = [
            Client(id=self.client_ids[key], comment=None, created_at=self.clock.now(), **item)
            for key, item in fixtures["clients"].items()
        ]
        self.emulator = ScenarioEmulator(
            self.clock,
            doctors,
            {TOKEN: clients},
            limit=external_limit,
            faults=dialog.setup.get("faults", ()),
        )
        for index, item in enumerate(dialog.setup.get("visits", ()), 1):
            self.emulator.records[TOKEN][index] = Visit(
                id=index,
                client_id=self.client_ids[item["client"]],
                doctor=self.emulator.doctors[self.doctor_ids[item["doctor"]]],
                scheduled_at=moment(item["at"]),
                status=VisitStatus.PLANNED,
                created_at=(
                    moment(item["created_at"])
                    if item.get("created_at")
                    else self.clock.now() - timedelta(days=2)
                ),
                updated_at=self.clock.now(),
            )
        self.initial_visit = self.emulator.records[TOKEN].get(1)
        self.initial_records = self.snapshot()
        slots_by_doctor = fixtures["slots"] | dialog.setup.get("slots", {})
        slots = tuple(
            Slot(
                doctor_id=self.doctor_ids[key],
                specialty=fixtures["doctors"][key]["specialty"],
                scheduled_at=moment(at),
            )
            for key, times in slots_by_doctor.items()
            for at in times
        )
        start = self.clock.now() - timedelta(days=1)
        finish = self.clock.now() + timedelta(days=30)
        access = []
        for key, doctor in fixtures["doctors"].items():
            if not slots_by_doctor.get(key):
                continue
            for client_key in fixtures["clients"]:
                # Допуск задаётся только явно вымышленной фикстурой, не выводится из визитов.
                granted = doctor["specialty"] in fixtures["access"]["direct_profiles"]
                granted |= any(
                    r["client"] == client_key
                    and r["specialty"] == doctor["specialty"]
                    and moment(r["issued"] + " 00:00")
                    <= self.clock.now()
                    < moment(r["issued"] + " 00:00") + timedelta(days=r["valid_days"])
                    for r in dialog.setup.get("referrals", ())
                )
                if granted:
                    access.append(
                        AccessFact(
                            client_id=self.client_ids[client_key],
                            doctor_id=self.doctor_ids[key],
                            state="allowed",
                            valid_from=start,
                            valid_until=finish,
                            source="Вымышленный допуск фикстуры T07",
                        )
                    )
        document = ScheduleDocument(
            schema_version=1,
            timezone="GMT+4",
            source="Синтетическое расписание tests/scenarios",
            generated_at=start,
            expires_at=finish,
            slots=slots,
            access=tuple(access),
        )
        self.schedule = JsonSchedule(
            document, self.clock, hashlib.sha256(document.model_dump_json().encode()).hexdigest()
        )
        self.dialogue_store = MemoryDialogueStore()
        self.handoff_store = MemoryHandoffStore()
        self.handoffs = None
        self.booking_store = MemoryBookingStore(guard_reader=self.dialogue_store)
        self.cancellation_store = MemoryCancellationStore(
            self.booking_store.ledger, self.dialogue_store
        )
        self.move_store = MemoryMoveStore(self.booking_store.ledger, self.dialogue_store)
        self.budget = (
            MemoryBudget(self.clock, internal_limit)
            if internal_limit is not None
            else EmulatorBudget()
        )
        self.settings = Settings(
            _env_file=None,
            app_mode="offline_test",
            backend_demo_enabled=True,
            dialogue_mode="natural",
            dialogue_storage="memory",
            web_access_enabled=False,
        )
        self.service = None
        self.reminder_store = None
        self.reminder_executor = None
        self.reminder_dialer = None
        self.reminder_attempt_id = None
        self.outbound_reply = None
        self.background_catalog = dialog.setup.get("background_catalog", False)
        self.budget_profile = (
            f"memory_{internal_limit}_server_{external_limit}_per_min"
            if internal_limit is not None
            else "functional_unlimited_emulator"
        )
        self.call_id = "scenario"

    def snapshot(self):
        return {
            identifier: item.model_dump(mode="json")
            for identifier, item in self.emulator.records[TOKEN].items()
        }

    @asynccontextmanager
    async def application(self):
        @asynccontextmanager
        async def factory(settings, clock):
            async with httpx.AsyncClient(
                base_url="https://scenario.invalid/api/v1/",
                headers={"Authorization": "Bearer " + TOKEN},
                transport=self.emulator.transport(),
            ) as client:
                mis = HttpMedicalSystem(
                    client, self.budget, elapsed=lambda: self.clock.now().timestamp()
                )
                if self.background_catalog:
                    background = HttpMedicalSystem(
                        client, self.budget, elapsed=lambda: self.clock.now().timestamp()
                    )
                    await background.doctors(fresh=True)
                    for record in self.emulator.requests:
                        record.source = "background"
                reminder_actions = None
                if self.dialog.setup.get("channel") == "outbound":
                    self.reminder_store = MemoryReminderStore(guard_reader=self.dialogue_store)
                    client_id = self.client_ids[self.dialog.setup["call"]["client"]]
                    self.emulator.clients[TOKEN][client_id] = self.emulator.clients[TOKEN][
                        client_id
                    ].model_copy(update={"comment": "Прежняя заметка регистратора"})
                    await self.reminder_store.save_contact(
                        Contact(client_id=client_id, endpoint=REMINDER_ENDPOINT)
                    )
                    calendar = ClinicCalendar(covered=(date(2026, 1, 1), date(2027, 12, 31)))
                    marks = ReminderMarks(
                        mis,
                        self.reminder_store,
                        self.clock,
                        owner=REMINDER_OWNER,
                        require_dialogue_guard=True,
                    )
                    self.reminder_dialer = ScenarioDialer(self)
                    self.reminder_executor = ReminderExecutor(
                        self.reminder_store,
                        mis,
                        calendar,
                        self.clock,
                        self.reminder_dialer,
                        owner=REMINDER_OWNER,
                        allowed_endpoints={REMINDER_ENDPOINT},
                        marks=marks,
                    )
                    reminder_actions = ReminderActionAdapter(
                        self.reminder_executor, marks, self.reminder_store, self.clock
                    )
                self.service = assemble_dialogue(
                    settings,
                    clock,
                    mis,
                    self.schedule,
                    self.booking_store,
                    self.dialogue_store,
                    self.cancellation_store,
                    self.move_store,
                    b"fictional-scenario-hash-key-longer-than-32",
                    reminder_actions=reminder_actions,
                    paid_prices={
                        item["service"]: item["price"]
                        for item in self.dialog.setup.get("prices", ())
                    },
                )
                self.handoffs = HandoffService(
                    self.handoff_store,
                    mis,
                    clock,
                    b"fictional-scenario-hash-key-longer-than-32",
                    HandoffSettings(enabled=True, callback_enabled=True),
                )
                self.service.handoffs = self.handoffs
                if self.reminder_dialer is not None:
                    self.reminder_dialer.executor = self.reminder_executor
                yield self.service

        app = create_app(
            self.settings,
            clock=self.clock,
            database_factory=lambda _: NoExternalDatabase(),
            dialogue_factory=factory,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://scenario.invalid"
            ) as client:
                yield client

    async def start_outbound(self):
        """Use T11 selection/originate path; only the telephone leg is synthetic."""
        if self.reminder_executor is None:
            return None
        await self.reminder_executor.tick()
        if self.reminder_dialer.context is None:
            return None
        self.reminder_attempt_id = self.reminder_dialer.attempt_id
        return self.outbound_reply

    def matches_visit(self, record, reference, field="at"):
        return (
            record["client_id"] == self.client_ids[reference["client"]]
            and record["doctor"]["id"] == self.doctor_ids[reference["doctor"]]
            and record["scheduled_at"] == moment(reference[field]).strftime(STAND_TIME)
        )


class ScenarioDialer:
    """Synthetic answered phone leg; it never supplies a dialogue reply or outcome."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.executor = None
        self.context = None
        self.attempt_id = None

    async def call_out(self, attempt_id, endpoint, context, channel_id):
        if endpoint != REMINDER_ENDPOINT or not channel_id:
            raise ValueError("Некорректный вымышленный телефонный канал")
        self.attempt_id = attempt_id
        self.context = context
        if not await self.executor.answered(attempt_id):
            return
        self.runtime.outbound_reply = await self.runtime.service.open_outbound(
            self.runtime.call_id, context
        )
