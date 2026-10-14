"""Отмена через HTTP создаёт очередь только после окончательной сверки МИС."""

import pytest
from fixture_bridge import pipeline, turn

from app.outbox.memory import MemoryOutboxStore
from app.storage.outbox import PostgresOutboxStore


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def queue_engine(request):
    return None if request.param == "memory" else request.getfixturevalue("pg_engine")


@pytest.mark.parametrize("lost_response", [False, True])
async def test_confirmed_cancel_and_reconciliation_publish_events_once(queue_engine, lost_response):
    setup = {"visits": [{"client": "ivanov_sp", "doctor": "belova", "at": "02.10.2026 10:40"}]}
    async with pipeline(setup, pg_engine=queue_engine) as (client, service, emulator, *_):
        cancellation = service.conversation.cancellation.cancellation
        store = (
            MemoryOutboxStore(cancellation.store.ledger)
            if queue_engine is None
            else PostgresOutboxStore(queue_engine)
        )
        await turn(client, 1, "Отмените запись к лору")
        await turn(client, 2, "Иванов Сергей Петрович, третье января девяностого")
        prepared = await turn(client, 3, "Отменить, уже не нужно")
        assert prepared["code"] == "CANCEL_CONFIRMATION_REQUIRED"
        assert await store.list() == ()
        await turn(client, 4, "Да", final=False)
        assert await store.list() == ()
        if lost_response:
            emulator.next_failure = "lost_response"
        result = await turn(client, 4, "Да")
        assert await turn(client, 4, "Да") == result
        if lost_response:
            assert result["code"] == "CANCEL_RECONCILING" and await store.list() == ()
            result = await turn(client, 5, "Проверьте отмену")
            assert await turn(client, 5, "Проверьте отмену") == result
        assert result["code"] == "CANCELLED"
        jobs = await store.list()
        assert len(jobs) == 2 and len({j.event_id for j in jobs}) == 2
        assert {j.source_id for j in jobs} == {prepared["cancel_proposal"]["action_id"]}
        assert not any("SMS" in text or "отправлен" in text for text in result["utterances"])
        assert emulator.patch_count == 1
