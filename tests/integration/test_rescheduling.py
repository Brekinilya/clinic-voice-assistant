"""С-03 через текстовый HTTP: старая запись сохраняется до подтверждённой новой."""

import asyncio
from datetime import timedelta

import pytest
import yaml
from fixture_bridge import SCENARIOS, pipeline, turn

from app.booking.models import Approval, BookingError, Slot
from app.cancellation.models import summary
from app.mis.budget import MemoryBudget
from app.mis.models import Doctor, VisitStatus, parse_datetime
from app.mis.ports import MisError
from app.outbox.memory import MemoryOutboxStore
from app.outbox.models import EventKind
from app.rescheduling.models import MoveState
from app.storage.outbox import PostgresOutboxStore

IDENTITY = "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого"
SETUP = {"visits": [{"client": "smirnova_ov", "doctor": "kuznetsova", "at": "01.10.2026 08:45"}]}


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def move_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


async def prepare(client):
    assert (await turn(client, 1, "Я записана на четверг, хочу перенести."))[
        "code"
    ] == "IDENTIFICATION_REQUIRED"
    assert (await turn(client, 2, IDENTITY))["code"] == "MOVE_SOURCE_CONFIRMATION"
    options = await turn(client, 3, "Да, на следующую неделю.")
    assert options["code"] == "MOVE_OPTIONS", options
    assert len(options["options"]) == 3
    prepared = await turn(client, 4, "Вторник.")
    assert prepared["code"] == "MOVE_CONFIRMATION_REQUIRED", prepared
    return prepared


def outbox(service, engine):
    return (
        MemoryOutboxStore(service.booking.store.ledger)
        if engine is None
        else PostgresOutboxStore(engine)
    )


async def test_original_c03_yaml_and_exact_wire_order(move_engine):
    case = yaml.safe_load((SCENARIOS / "main/c03_reschedule.yaml").read_text(encoding="utf-8"))
    async with pipeline(case["setup"], pg_engine=move_engine) as (client, service, emulator, *_):
        requests = []

        async def capture(request):
            requests.append((request.method, request.url.path))
            if request.method == "PATCH":
                assert (
                    emulator.write_count == 1
                    and emulator.records["demo"][2].status == VisitStatus.PLANNED
                )

        service.booking.mis._client.event_hooks["request"].append(capture)
        number = 0
        for step in case["steps"]:
            if "patient" not in step:
                continue
            number += 1
            result = await turn(client, number, step["patient"])
            if step.get("say") == "reschedule.offer":
                assert len(result["options"]) == 3
            if step.get("say") == "reschedule.confirm":
                assert emulator.write_count == emulator.patch_count == 0
        assert result["code"] == "RESCHEDULED", result
        assert emulator.records["demo"][1].status == VisitStatus.CANCELLED
        new = emulator.records["demo"][2]
        assert new.status == VisitStatus.PLANNED and (
            new.scheduled_at.month,
            new.scheduled_at.day,
            new.scheduled_at.hour,
        ) == (10, 6, 12)
        assert emulator.write_count == emulator.patch_count == 1
        assert await turn(client, number, case["steps"][-1]["patient"]) == result
        writes = [method for method, _ in requests if method != "GET"]
        assert writes == ["POST", "PATCH"]
        index = requests.index(("PATCH", "/api/v1/visits/1"))
        assert ("GET", "/api/v1/visits/2") in requests[:index]
        jobs = await outbox(service, move_engine).list()
        assert len(jobs) == 2 and {j.payload.kind for j in jobs} == {
            EventKind.RESCHEDULE_SMS,
            EventKind.SLOT_RELEASED,
        }


async def test_final_refusal_preserves_old_and_never_writes(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, _, emulator, *_):
        before = emulator.records["demo"][1]
        await prepare(client)
        assert (await turn(client, 5, "Нет"))["code"] == "CALL_ENDED"
        assert emulator.records["demo"][1] == before
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize("failure", ["lost_response", "server_error", "invalid_response"])
async def test_unknown_post_requires_readback_and_new_yes_before_old_cancel(move_engine, failure):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        before = emulator.records["demo"][1]
        await prepare(client)
        emulator.next_failure = failure
        pending = await turn(client, 5, "Да")
        assert pending["code"] == "MOVE_PENDING"
        assert await turn(client, 5, "Да") == pending
        assert emulator.write_count == 1 and emulator.patch_count == 0
        assert (
            emulator.records["demo"][1] == before
            and await outbox(service, move_engine).list() == ()
        )
        renewed = await turn(client, 6, "Проверьте перенос")
        assert renewed["code"] == "MOVE_CONFIRMATION_REQUIRED"
        assert (
            renewed["move_proposal"]["stage"] == "finish"
            and renewed["move_proposal"]["new_visit_id"] == 2
        )
        assert emulator.patch_count == 0
        assert (await turn(client, 7, "Да"))["code"] == "RESCHEDULED"
        assert emulator.write_count == emulator.patch_count == 1


@pytest.mark.parametrize("failure", ["lost_response", "server_error", "invalid_response"])
async def test_unknown_patch_reads_both_visits_and_never_repeats(move_engine, failure):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        await prepare(client)

        async def fail_patch(request):
            if request.method == "PATCH":
                emulator.next_failure = failure

        service.booking.mis._client.event_hooks["request"].append(fail_patch)
        pending = await turn(client, 5, "Да")
        assert pending["code"] == "MOVE_PENDING"
        assert emulator.write_count == emulator.patch_count == 1
        assert await outbox(service, move_engine).list() == ()
        assert (await turn(client, 6, "Проверьте перенос"))["code"] == "RESCHEDULED"
        assert emulator.patch_count == 1 and len(await outbox(service, move_engine).list()) == 2


@pytest.mark.parametrize("step", ["post", "patch"])
async def test_known_failure_keeps_old_and_escalates_without_compensation(move_engine, step):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)

        async def reject(*args):
            raise MisError("CONFLICT")

        if step == "post":
            service.booking.mis.create_visit = reject
        else:
            service.booking.mis.change_visit = reject
        result = await turn(client, 5, "Да")
        assert result["code"] == "MOVE_NEEDS_OPERATOR" and result["transfer"]["route"] == "R-06"
        assert result["transfer"]["action_id"] == prepared["move_proposal"]["action_id"]
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.write_count == (1 if step == "patch" else 0) and emulator.patch_count == 0
        assert await outbox(service, move_engine).list() == ()


@pytest.mark.parametrize("interruption", ["partial", "end", "red_flag"])
async def test_new_input_after_post_revokes_permission_to_cancel_old(move_engine, interruption):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        prepared = await prepare(client)
        started, finish = asyncio.Event(), asyncio.Event()
        original = service.booking.mis.create_visit

        async def delayed(data):
            result = await original(data)
            started.set()
            await finish.wait()
            return result

        service.booking.mis.create_visit = delayed
        task = asyncio.create_task(turn(client, 5, "Да"))
        await started.wait()
        if interruption == "end":
            assert (await client.post("/api/dialog/scenario/end")).status_code == 200
        else:
            await turn(
                client,
                6,
                "Нет, другое время" if interruption == "partial" else "не могу дышать",
                final=False,
            )
        finish.set()
        result = await task
        assert result["code"] in {"TURN_INTERRUPTED", "CALL_ENDED", "EMERGENCY"}
        assert emulator.write_count == 1 and emulator.patch_count == 0
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        action = await service.conversation.rescheduling.service.store.get(
            prepared["move_proposal"]["action_id"]
        )
        assert action.state == MoveState.NEW_CREATED
        if interruption == "red_flag":
            assert result["transfer"]["action_id"] == action.action_id
        assert await outbox(service, move_engine).list() == ()


async def test_shared_quota_pauses_between_steps_and_needs_new_confirmation(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, clock, *_):
        await prepare(client)
        service.booking.mis._budget = MemoryBudget(clock)
        waiting = await turn(client, 5, "Да")
        assert waiting["code"] == "WAIT_MIS", waiting
        assert waiting["move_proposal"]["stage"] == "finish"
        assert emulator.write_count == 1 and emulator.patch_count == 0
        clock.value += timedelta(seconds=60)
        assert (await turn(client, 6, "Да"))["code"] == "RESCHEDULED"
        assert emulator.write_count == emulator.patch_count == 1


@pytest.mark.parametrize("change", ["patient", "date", "status"])
async def test_new_visit_must_still_match_before_old_can_be_cancelled(move_engine, change):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        await prepare(client)
        original = service.booking.mis.create_visit

        async def changed(data):
            result = await original(data)
            updates = {
                "patient": {"client_id": result.client_id + 1},
                "date": {"scheduled_at": result.scheduled_at + timedelta(hours=1)},
                "status": {"status": VisitStatus.CANCELLED},
            }
            emulator.records["demo"][result.id] = result.model_copy(update=updates[change])
            return result

        service.booking.mis.create_visit = changed
        assert (await turn(client, 5, "Да"))["code"] == "MOVE_NEEDS_OPERATOR"
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.write_count == 1 and emulator.patch_count == 0


async def test_no_active_visit_can_start_normal_booking(move_engine):
    async with pipeline(pg_engine=move_engine) as (client, _, emulator, *_):
        await turn(client, 1, "Хочу перенести запись")
        assert (await turn(client, 2, IDENTITY))["code"] == "NO_ACTIVE_VISITS"
        assert (await turn(client, 3, "Да"))["code"] == "SPECIALTY_REQUIRED"
        assert (await turn(client, 4, "К терапевту"))["phase"] == "offered"
        assert emulator.write_count == emulator.patch_count == 0


async def test_known_local_booking_can_be_moved_then_cancelled(move_engine):
    async with pipeline(pg_engine=move_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, IDENTITY)
        booked = await turn(client, 3, "Первый")
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        assert (await turn(client, 5, "Перенесите мою запись"))[
            "code"
        ] == "MOVE_SOURCE_CONFIRMATION"
        await turn(client, 6, "Да, на следующую неделю")
        moved = await turn(client, 7, "Вторник")
        assert (await turn(client, 8, "Да"))["code"] == "RESCHEDULED"
        assert (
            await service.booking.store.get(booked["proposal"]["action_id"])
        ).state == "archived"
        assert (await service.booking.store.get(moved["move_proposal"]["action_id"])).visit_id == 2
        assert (await turn(client, 9, "Отмените мою запись"))["code"] == "RESCHEDULE_OFFERED"
        await turn(client, 10, "Отменить, уже не нужно")
        assert (await turn(client, 11, "Да"))["code"] == "CANCELLED"
        assert emulator.write_count == 2 and emulator.patch_count == 2


@pytest.mark.parametrize(
    "text", ["Вы робот?", "Не записывайте разговор", "Какую таблетку принять?"]
)
async def test_other_question_yes_answers_only_the_repeated_explicit_question(move_engine, text):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, _, emulator, *_):
        prepared = await prepare(client)
        result = await turn(client, 5, text)
        assert emulator.write_count == emulator.patch_count == 0
        if text == "Какую таблетку принять?":
            # «Да» на предложение терапевта не подтверждает прежний перенос.
            assert result["question"]["key"] == "medical_booking"
            await turn(client, 6, "Да")
            assert emulator.write_count == emulator.patch_count == 0
            assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        else:
            # Служебная реплика теперь перечитывает ВСЁ предложение переноса,
            # поэтому новое «да» относится к этому явному вопросу.
            assert result["question"] == prepared["question"]
            assert result["utterances"][-len(prepared["utterances"]) :] == prepared["utterances"]
            assert (await turn(client, 6, "Да"))["code"] == "RESCHEDULED"
            assert emulator.write_count == emulator.patch_count == 1


async def test_other_visit_is_unchanged_when_one_of_two_is_moved(move_engine):
    setup = {
        "visits": [
            *SETUP["visits"],
            {"client": "smirnova_ov", "doctor": "belova", "at": "02.10.2026 10:40"},
        ]
    }
    async with pipeline(setup, pg_engine=move_engine) as (client, _, emulator, *_):
        other = emulator.records["demo"][2]
        await turn(client, 1, "Хочу перенести одну из записей")
        assert (await turn(client, 2, IDENTITY))["code"] == "MOVE_CHOICE_REQUIRED"
        assert (await turn(client, 3, "Которая к терапевту"))["code"] == "MOVE_SOURCE_CONFIRMATION"
        await turn(client, 4, "Да, на следующую неделю")
        await turn(client, 5, "Вторник")
        assert (await turn(client, 6, "Да"))["code"] == "RESCHEDULED"
        assert emulator.records["demo"][2] == other


async def test_concurrent_final_yes_creates_and_cancels_once(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, _, emulator, *_):
        await prepare(client)
        results = await asyncio.gather(*(turn(client, 5, "Да") for _ in range(5)))
        assert any(r["code"] == "RESCHEDULED" for r in results)
        assert emulator.write_count == emulator.patch_count == 1


async def test_fio_refinement_revokes_old_target_and_does_not_write(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, _, emulator, *_):
        await prepare(client)
        await turn(client, 5, "Да, Иванов Сергей Петрович, третье января девяностого")
        assert emulator.write_count == emulator.patch_count == 0


async def test_cancellation_offer_can_start_rescheduling(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, _, emulator, *_):
        await turn(client, 1, "Отмените запись")
        assert (await turn(client, 2, IDENTITY))["code"] == "RESCHEDULE_OFFERED"
        assert (await turn(client, 3, "Лучше перенесите"))["code"] == "MOVE_SOURCE_CONFIRMATION"
        await turn(client, 4, "Да, на следующую неделю")
        await turn(client, 5, "Вторник")
        assert (await turn(client, 6, "Да"))["code"] == "RESCHEDULED"
        assert emulator.write_count == emulator.patch_count == 1


async def test_missing_server_guard_and_stale_old_visit_never_create(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        held = await prepare(client)
        with pytest.raises(BookingError, match="DIALOGUE_GUARD_REQUIRED"):
            await service.conversation.rescheduling.service.confirm(
                Approval(
                    call_id="scenario",
                    action_id=held["move_proposal"]["action_id"],
                    revision=held["move_proposal"]["revision"],
                    turn_id=5,
                    is_final=True,
                    permitted=True,
                )
            )
        old = emulator.records["demo"][1]
        emulator.records["demo"][1] = old.model_copy(
            update={"scheduled_at": old.scheduled_at + timedelta(hours=1)}
        )
        assert (await turn(client, 5, "Да"))["code"] == "VISIT_CHANGED"
        assert emulator.write_count == emulator.patch_count == 0
        assert (await turn(client, 6, "Повторите"))["code"] == "MOVE_SOURCE_CONFIRMATION"


async def test_no_slots_preserves_old_visit(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Хочу перенести запись")
        await turn(client, 2, IDENTITY)
        service.booking.schedule._slots = tuple(
            s for s in service.booking.schedule.slots() if s.scheduled_at.day < 5
        )
        assert (await turn(client, 3, "Да, на следующую неделю"))["code"] == "NO_SLOTS"
        assert (await turn(client, 4, "Да"))["code"] == "NO_SLOTS"
        assert emulator.records["demo"][1].status == VisitStatus.PLANNED
        assert emulator.write_count == emulator.patch_count == 0


async def test_cancellation_service_cannot_bypass_rescheduling(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, *_):
        await prepare(client)
        selected = summary(emulator.records["demo"][1])
        with pytest.raises(BookingError, match="RESCHEDULE_REQUIRES_WORKFLOW"):
            await service.conversation.cancellation.cancellation.prepare(
                "scenario", emulator.records["demo"][1].client_id, selected, "reschedule"
            )
        assert emulator.write_count == emulator.patch_count == 0


async def test_another_eligible_doctor_of_same_specialty_can_be_offered(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, clock, _, *_):
        old = emulator.records["demo"][1]
        other_client = next(c for c in emulator.clients["demo"] if c != old.client_id)
        for number, slot in enumerate(service.booking.schedule.slots(), 10):
            if slot.doctor_id == old.doctor.id and 5 <= slot.scheduled_at.day <= 11:
                emulator.records["demo"][number] = old.model_copy(
                    update={
                        "id": number,
                        "client_id": other_client,
                        "scheduled_at": slot.scheduled_at,
                    }
                )
        doctor_id = max(emulator.doctors) + 1
        emulator.doctors[doctor_id] = Doctor(
            id=doctor_id,
            full_name="Тестов Алексей Петрович",
            specialty=old.doctor.specialty,
            is_active=True,
        )
        slot = Slot(
            doctor_id=doctor_id,
            specialty=old.doctor.specialty,
            scheduled_at=parse_datetime("05.10.2026 10:00"),
        )
        service.booking.schedule._slots += (slot,)
        service.booking.schedule.allowed.add((old.client_id, doctor_id))
        service.conversation.rescheduling.rooms = None
        await turn(client, 1, "Хочу перенести запись")
        await turn(client, 2, IDENTITY)
        choices = await turn(client, 3, "Да, на следующую неделю")
        assert len(choices["options"]) == 1 and choices["options"][0]["doctor_id"] == doctor_id
        assert "Тестов" in " ".join(choices["utterances"])
        await turn(client, 4, "Первый")
        assert (await turn(client, 5, "Да"))["code"] == "RESCHEDULED"
        assert emulator.records["demo"][1].status == VisitStatus.CANCELLED


async def test_reschedule_notification_is_recorded_without_cancellation_sms(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, clock, *_):
        await prepare(client)
        assert (await turn(client, 5, "Да"))["code"] == "RESCHEDULED"
        from sqlalchemy import select

        from app.outbox.delivery import MemoryJournalDelivery
        from app.outbox.worker import OutboxWorker
        from app.storage.outbox_delivery import PostgresJournalDelivery
        from app.storage.schema import delivery_journal

        delivery = (
            MemoryJournalDelivery(service.booking.store.ledger, clock)
            if move_engine is None
            else PostgresJournalDelivery(move_engine, clock)
        )
        worker = OutboxWorker(outbox(service, move_engine), delivery, clock)
        for _ in range(2):
            assert (await worker.run_once()).receipt.mode == "demo_journal"
        if move_engine is None:
            entries = [
                e.model_dump(mode="json") for e in service.booking.store.ledger.deliveries.values()
            ]
        else:
            async with move_engine.connect() as connection:
                entries = list(
                    (await connection.execute(select(delivery_journal.c.entry))).scalars()
                )
        sms = next(e for e in entries if e["payload"]["kind"] == "reschedule_sms")
        assert "06.10 в 12:00, каб. 214" in sms["text"]
        assert "Кузнецова" in sms["text"] and "терапевт" not in sms["text"]
        assert "Смирнова" not in sms["text"] and "Прежняя запись отменена" in sms["text"]
        assert len(entries) == 2


async def test_expired_local_reservation_returns_to_choice_without_a_write(move_engine):
    async with pipeline(SETUP, pg_engine=move_engine) as (client, service, emulator, clock, *_):
        held = await prepare(client)
        clock.value += timedelta(seconds=90)
        action = await service.conversation.rescheduling.service.store.get(
            held["move_proposal"]["action_id"]
        )
        assert await service.booking.store.available(action.slot, clock.now())
        answer = await turn(client, 5, "Да")
        assert answer["code"] == "MOVE_OPTIONS"
        assert emulator.write_count == emulator.patch_count == 0
