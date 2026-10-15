"""Внешняя сборка: настоящий индекс, изолированный транспорт и явный PostgreSQL."""

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from app.demo import DemoClock, demo_emulator
from app.dialogue import live
from app.dialogue.bootstrap import open_dialogue_backend
from app.dialogue.models import Turn
from app.mis.budget import MemoryBudget
from app.scheduling.bootstrap import token_scope
from app.scheduling.importer import import_workbook
from app.scheduling.models import ScheduleImportError
from app.settings import Settings
from app.storage.schema import SCHEMA_VERSION
from tests.test_schedule_import import catalog, workbook


def settings_for(path):
    return Settings(
        _env_file=None,
        app_mode="hackathon",
        mis_base_url="https://offline.invalid/api/v1",
        mis_token="fictional-team-token",
        dialogue_enabled=True,
        dialogue_mode="natural",
        dialogue_storage="postgres",
        dialogue_hash_key="fictional-stable-dialogue-key-over-32-bytes",
        schedule_index_path=path,
    )


def prepare_index(tmp_path):
    clock = DemoClock()
    path = tmp_path / "schedule.sqlite"
    settings = settings_for(path)
    document = catalog(clock, mode="hackathon", token_scope=token_scope(settings))
    import_workbook(workbook(tmp_path / "source.xlsx"), document, path, clock.now())
    return clock, settings, document


class Engine:
    closed = False

    @asynccontextmanager
    async def connect(self):
        class Rows:
            def scalars(self):
                return [SCHEMA_VERSION]

        class Connection:
            async def execute(self, query):
                return Rows()

        yield Connection()

    async def dispose(self):
        self.closed = True


@pytest.mark.parametrize("changed_catalog", [False, True])
async def test_live_assembly_checks_catalog_and_closes_resources(
    tmp_path, monkeypatch, changed_catalog
):
    clock, settings, document = prepare_index(tmp_path)
    doctor = document.doctors[0]
    if changed_catalog:
        doctor = doctor.model_copy(update={"full_name": "Другой Врач Петрович"})
    emulator = demo_emulator(clock, doctors=(doctor,))
    client = httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        transport=emulator.transport(),
        headers={"Authorization": "Bearer demo"},
    )
    engine = Engine()
    monkeypatch.setattr(live, "backend_engine", lambda _: engine)
    monkeypatch.setattr(live.HttpMedicalSystem, "live_client", lambda _: client)
    monkeypatch.setattr(live, "PostgresBudget", lambda *args: MemoryBudget(clock))
    try:
        async with open_dialogue_backend(settings, clock) as service:
            assert not changed_catalog
            assert type(service.store).__name__ == "PostgresDialogueStore"
            assert type(service.booking.store).__name__ == "PostgresBookingStore"
            assert service.booking.require_dialogue_guard
            assert service.conversation is not None
    except RuntimeError as error:
        assert changed_catalog and "справочник" in str(error)
    assert engine.closed and client.is_closed and emulator.write_count == 0


async def test_wrong_team_index_is_rejected_before_any_network(tmp_path, monkeypatch):
    clock, settings, _ = prepare_index(tmp_path)
    wrong = Settings(
        **(settings.model_dump() | {"mis_token": "another-team-token"}), _env_file=None
    )

    def forbidden(*args):
        pytest.fail("неподходящий индекс не должен открывать базу или МИС")

    monkeypatch.setattr(live, "backend_engine", forbidden)
    monkeypatch.setattr(live.HttpMedicalSystem, "live_client", forbidden)
    with pytest.raises(ScheduleImportError, match="INDEX_SCOPE_MISMATCH"):
        async with open_dialogue_backend(wrong, clock):
            pytest.fail("индекс другой команды принят")


@pytest.mark.postgres
async def test_external_dialogue_booking_uses_durable_stores(tmp_path, monkeypatch, pg_engine):
    clock, settings, document = prepare_index(tmp_path)
    emulator = demo_emulator(clock, doctors=document.doctors)
    client = httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        transport=emulator.transport(),
        headers={"Authorization": "Bearer demo"},
    )
    monkeypatch.setattr(live, "backend_engine", lambda _: pg_engine)
    monkeypatch.setattr(live.HttpMedicalSystem, "live_client", lambda _: client)
    async with open_dialogue_backend(settings, clock) as service:
        for number, text in enumerate(
            ("Запишите к хирургу", "Тестов Александр Примерович, 01.01.1990", "Первый"), 1
        ):
            result = await service.handle("external-path", Turn(turn_id=number, text=text))
        assert result.phase == "held" and emulator.write_count == 0
        # Следующая минута отделяет проверки запуска от квоты операции подтверждения.
        clock.value += timedelta(minutes=1)
        await service.input_started("external-path", 4)
        confirmed = await service.handle("external-path", Turn(turn_id=4, text="Да"))
        assert confirmed.code == "BOOKED" and emulator.write_count == 1
        assert (await service.store.inspect("external-path")).visit_id == confirmed.visit_id
