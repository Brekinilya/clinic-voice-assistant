"""Ограниченные вычисления: отмена ожидания не освобождает занятый нативный поток."""

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


class InferenceBusy(RuntimeError):
    """Нет места в ограниченной очереди либо исполнитель уже закрыт."""


@dataclass
class _Job:
    priority: int
    number: int
    run: Callable[[], Any]
    result: asyncio.Future


class InferenceQueue:
    """Один поток модели и короткая очередь, общие для звонков приложения.

    Поток получает следующее задание только после фактического окончания прошлого.
    Таймаут/перебивание отменяют получение результата, а не создают новый поток.
    """

    def __init__(self, name: str, capacity: int = 2):
        if capacity < 1:
            raise ValueError("ёмкость очереди должна быть положительной")
        self.capacity = capacity
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-" + name)
        self._pending: list[_Job] = []
        self._running = False
        self._closed = False
        self._number = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def queued(self) -> int:
        return len(self._pending)

    async def run(self, function: Callable, *args, timeout: float, priority: int = 0):
        if self._closed:
            raise InferenceBusy()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._number += 1
        job = _Job(priority, self._number, lambda: function(*args), future)
        if not self._running:
            self._start(job, loop)
        else:
            if len(self._pending) >= self.capacity:
                worst = max(self._pending, key=lambda item: (item.priority, item.number))
                if job.priority >= worst.priority:
                    raise InferenceBusy()
                # Итоговая фраза вытесняет ещё не запущенное промежуточное распознавание.
                self._pending.remove(worst)
                if not worst.result.done():
                    worst.result.set_exception(InferenceBusy())
            self._pending.append(job)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Исключение не остаётся «непрочитанным» при гонке с отменой.
            if job in self._pending:
                self._pending.remove(job)

    def _start(self, job: _Job, loop: asyncio.AbstractEventLoop) -> None:
        self._running = True
        native = self._pool.submit(job.run)

        def finished(result):
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._complete, job, result, loop)
                except RuntimeError:
                    # Между проверкой и callback приложение могло завершить event loop.
                    pass

        native.add_done_callback(finished)

    def _complete(self, job: _Job, native: Future, loop: asyncio.AbstractEventLoop) -> None:
        self._running = False
        if not job.result.done():
            # Результат или исключение потока передаём ожидающему без перехвата
            if native.cancelled():
                job.result.cancel()
            elif (error := native.exception()) is not None:
                job.result.set_exception(error)
            else:
                job.result.set_result(native.result())
        if self._pending and not self._closed:
            following = min(self._pending, key=lambda item: (item.priority, item.number))
            self._pending.remove(following)
            self._start(following, loop)

    def close(self) -> None:
        self._closed = True
        for job in self._pending:
            job.result.cancel()
        self._pending.clear()
        # Работающий PyTorch/ONNX вызов принудительно не прерывается. Новых заданий не будет.
        self._pool.shutdown(wait=False, cancel_futures=True)


class SpeechRuntime:
    """Общие очереди моделей и допуск фоновых ходов диалога на процесс приложения."""

    def __init__(self, queue_capacity: int = 2, dialogue_capacity: int = 4):
        self.asr = InferenceQueue("asr", queue_capacity)
        self.tts = InferenceQueue("tts", queue_capacity)
        self._dialogue_capacity = dialogue_capacity
        self._requests: set[asyncio.Task] = set()
        self._closed = False

    def request(self, coroutine: Coroutine) -> asyncio.Task:
        if self._closed or len(self._requests) >= self._dialogue_capacity:
            coroutine.close()
            raise InferenceBusy()
        task = asyncio.create_task(coroutine)
        self._requests.add(task)

        def finished(done):
            self._requests.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    def close(self, *, cancel_requests: bool = True) -> None:
        self._closed = True
        self.asr.close()
        self.tts.close()
        if cancel_requests:
            for task in self._requests:
                task.cancel()
