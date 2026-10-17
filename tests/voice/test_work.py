"""Проверяем фактическую занятость потоков, приоритет и предел очереди при отменах."""

import asyncio
import threading

import pytest

from app.voice.work import InferenceBusy, InferenceQueue, SpeechRuntime
from tests.voice.test_session import until


async def test_timeouts_do_not_release_the_still_running_native_worker():
    release = threading.Event()
    started = threading.Event()
    calls = []
    queue = InferenceQueue("test", capacity=1)

    def blocked():
        calls.append("blocked")
        started.set()
        release.wait(3)
        return "old"

    try:
        with pytest.raises(TimeoutError):
            await queue.run(blocked, timeout=0.03)
        assert started.is_set() and queue.running
        for _ in range(12):
            with pytest.raises(TimeoutError):
                await queue.run(lambda: calls.append("must_not_run"), timeout=0.002)
            assert queue.queued == 0 and queue.running
        assert calls == ["blocked"]
        release.set()
        await until(lambda: not queue.running)
        assert await queue.run(lambda: "new", timeout=0.5) == "new"
    finally:
        release.set()
        queue.close()


async def test_cancelling_the_waiter_keeps_native_execution_serial():
    release = threading.Event()
    started = threading.Event()
    calls = []
    queue = InferenceQueue("test", capacity=1)

    def blocked():
        calls.append("first")
        started.set()
        release.wait(3)

    first = asyncio.create_task(queue.run(blocked, timeout=2))
    try:
        await until(started.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(queue.run(lambda: calls.append("second"), timeout=1))
        await until(lambda: queue.queued == 1)
        assert calls == ["first"]
        release.set()
        await second
        assert calls == ["first", "second"]
    finally:
        release.set()
        queue.close()
        await asyncio.gather(first, return_exceptions=True)


async def test_final_speech_displaces_queued_partial_and_capacity_is_bounded():
    release = threading.Event()
    started = threading.Event()
    calls = []
    queue = InferenceQueue("test", capacity=1)

    def blocked():
        started.set()
        release.wait(3)

    first = asyncio.create_task(queue.run(blocked, timeout=2))
    tasks = [first]
    try:
        await until(started.is_set)
        partial = asyncio.create_task(
            queue.run(lambda: calls.append("partial"), timeout=1, priority=10)
        )
        tasks.append(partial)
        await until(lambda: queue.queued == 1)
        with pytest.raises(InferenceBusy):
            await queue.run(lambda: calls.append("overflow"), timeout=0.1, priority=10)
        final = asyncio.create_task(queue.run(lambda: calls.append("final"), timeout=1, priority=0))
        tasks.append(final)
        with pytest.raises(InferenceBusy):
            await partial
        assert queue.queued == 1
        release.set()
        await asyncio.gather(first, final)
        assert calls == ["final"]
    finally:
        release.set()
        queue.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_exception_in_model_does_not_poison_the_next_job():
    queue = InferenceQueue("test")

    def failed():
        raise ValueError("test")

    try:
        with pytest.raises(ValueError):
            await queue.run(failed, timeout=1)
        assert await queue.run(lambda: "ok", timeout=1) == "ok"
    finally:
        queue.close()


async def test_background_dialogue_jobs_have_a_shared_limit_until_completion():
    runtime = SpeechRuntime(dialogue_capacity=1)
    release = asyncio.Event()
    task = runtime.request(release.wait())
    try:
        with pytest.raises(InferenceBusy):
            runtime.request(release.wait())
        # Окончание локального ожидания не снимает допуск с фоновой операции.
        done, _ = await asyncio.wait({task}, timeout=0.01)
        assert not done
        with pytest.raises(InferenceBusy):
            runtime.request(release.wait())
        release.set()
        await task
        await asyncio.sleep(0)
        assert await runtime.request(asyncio.sleep(0, result="ok")) == "ok"
    finally:
        release.set()
        runtime.close()
        await asyncio.gather(task, return_exceptions=True)
