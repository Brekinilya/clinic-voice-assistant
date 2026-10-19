"""Задержки голосового шлюза на одной и нескольких параллельных сессиях — без SIP.

Реплики «пациента» синтезирует Silero (другим голосом, не голосом ассистента) и шлёт в
WebSocket /api/voice/{call_id} в реальном темпе кадрами по 20 мс; между фразами идёт тишина,
как от живого микрофона. Шлюз запускать с TELEPHONY_ENABLED=false: тестовый процесс не должен
перехватить ARI-приложение рабочего Asterisk.

    python bench/voice/load.py --url ws://127.0.0.1:18001 --sessions 2 --calls 3 \
        --out ../protocol/load-2.json

Результат: по каждой реплике — этапы, которые шлюз сам сообщает (timing), и задержка от конца
речи до первого байта ответа, измеренная клиентом. Итог — p50/p95 с числом наблюдений.
Синтезированная речь чище человеческой: распознавание и паузы здесь оптимистичнее живого звонка.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import threading
import time
from pathlib import Path

import websockets

from app.voice.engines import SileroSynthesizer, resample
from bench.voice.metrics import memory_mib

ROOT = Path(__file__).resolve().parents[2]
RATE = 16000
FRAME = RATE // 50 * 2  # 20 мс PCM 16 бит
# Один разговор записи на вымышленного пациента демо-стенда; финальное согласие не даём
SCRIPT = (
    "Здравствуйте, хочу записаться к терапевту.",
    "Иванов Иван Иванович.",
    "Первое января тысяча девятьсот девяностого года.",
    "Давайте в пятницу утром.",
    "Нет, спасибо, не надо.",
)
METRICS = ("silence_ms", "asr_ms", "dialog_ms", "tts_ms", "total_ms", "since_speech_ms")


def voices(speaker: str) -> list[bytes]:
    tts = SileroSynthesizer(ROOT / "models/silero/v5_5_ru.pt", speaker, 24000, threads=2)
    return [resample(tts.synthesize(text)[0], 24000, RATE) for text in SCRIPT]


class Call:
    def __init__(self, socket):
        self.socket = socket
        self.speech_ended: float | None = None
        self.turns: list[dict] = []
        self.current: dict = {}
        self.playing_until = 0.0
        self.ended = False
        self.reply = asyncio.Event()

    async def receive(self) -> None:
        try:
            await self._receive()
        except websockets.ConnectionClosed:
            pass
        self.ended = True
        self.reply.set()

    async def _receive(self) -> None:
        rate = 24000
        async for message in self.socket:
            now = time.monotonic()
            if isinstance(message, bytes):
                if self.speech_ended is not None and "first_audio_ms" not in self.current:
                    self.current["first_audio_ms"] = round((now - self.speech_ended) * 1000)
                self.playing_until = max(now, self.playing_until) + len(message) / 2 / rate
                continue
            event = json.loads(message)
            kind = event["type"]
            if kind == "speak":
                rate = event["sample_rate"]
            elif kind == "transcript" and event.get("final"):
                self.current["asr_status"] = event.get("status")
            elif kind == "reply":
                self.current["code"] = event.get("code")
                self.reply.set()
            elif kind == "timing":
                self.current.update({k: event[k] for k in METRICS if k in event})
            elif kind == "end":
                self.ended = True
                self.reply.set()

    async def wait_playback(self) -> None:
        while (left := self.playing_until - time.monotonic()) > 0:
            await asyncio.sleep(min(left, 0.1))


async def stream(socket, pcm: bytes) -> None:
    started = time.monotonic()
    for n, offset in enumerate(range(0, len(pcm), FRAME)):
        try:
            await socket.send(pcm[offset : offset + FRAME])
        except websockets.ConnectionClosed:
            return
        # Реальный темп: кадр за 20 мс, без накопления отставания
        await asyncio.sleep(max(0, started + (n + 1) * 0.02 - time.monotonic()))


async def silence(socket, stop: asyncio.Event) -> None:
    quiet = bytes(FRAME)
    next_at = time.monotonic()
    while not stop.is_set():
        try:
            await socket.send(quiet)
        except websockets.ConnectionClosed:
            # Шлюз сам закончил звонок (прощание, перевод): это нормальный исход
            return
        next_at += 0.02
        await asyncio.sleep(max(0, next_at - time.monotonic()))


async def one_call(url: str, name: str, phrases: list[bytes]) -> list[dict]:
    async with websockets.connect(f"{url}/api/voice/{name}", max_size=None) as socket:
        call = Call(socket)
        receiver = asyncio.create_task(call.receive())
        await asyncio.sleep(1.0)
        await call.wait_playback()
        for number, pcm in enumerate(phrases, 1):
            if call.ended:
                break
            call.current, call.speech_ended = {"turn": number}, None
            call.reply.clear()
            await stream(socket, pcm)
            call.speech_ended = time.monotonic()
            stop = asyncio.Event()
            filler = asyncio.create_task(silence(socket, stop))
            try:
                await asyncio.wait_for(call.reply.wait(), 30)
                # Ответ дошёл до конца: timing приходит после первой фразы синтеза
                await asyncio.sleep(0.3)
                await call.wait_playback()
            except TimeoutError:
                call.current["timeout"] = True
            stop.set()
            await filler
            call.turns.append(call.current)
        try:
            await socket.send(json.dumps({"type": "hangup"}))
        except websockets.ConnectionClosed:
            pass
        receiver.cancel()
        return call.turns


class Stats(threading.Thread):
    """CPU и память контейнера раз в 2 секунды, пока идёт замер."""

    def __init__(self, container: str | None):
        super().__init__(daemon=True)
        self.container = container
        self.samples: list[tuple[float, float]] = []
        self.done = threading.Event()

    def run(self) -> None:
        while self.container and not self.done.is_set():
            out = subprocess.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}} {{.MemUsage}}",
                    self.container,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.split()
            if out:
                cpu = float(out[0].rstrip("%"))
                self.samples.append((cpu, memory_mib(out[1])))
            self.done.wait(2)


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))]


def summary(rows: list[dict], stats: Stats) -> dict:
    result = {}
    for metric in (*METRICS, "first_audio_ms"):
        values = [row[metric] for row in rows if metric in row]
        if values:
            result[metric] = {
                "n": len(values),
                "p50": statistics.median(values),
                "p95": percentile(values, 0.95),
                "max": max(values),
            }
    if stats.samples:
        result["cpu_percent"] = {"n": len(stats.samples), "max": max(s[0] for s in stats.samples)}
        result["memory_mib"] = {"max": round(max(s[1] for s in stats.samples))}
    result["turns"] = len(rows)
    result["timeouts"] = sum(1 for row in rows if row.get("timeout"))
    result["codes"] = sorted({str(row.get("code")) for row in rows})
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="ws://127.0.0.1:18001")
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--calls", type=int, default=3, help="звонков на сессию")
    parser.add_argument("--speaker", default="aidar", help="голос пациента Silero")
    parser.add_argument("--container", help="имя контейнера для docker stats")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    phrases = voices(args.speaker)
    stats = Stats(args.container)
    stats.start()

    async def session(index: int) -> list[dict]:
        rows = []
        for call in range(args.calls):
            for row in await one_call(args.url, f"load-{index}-{call}", phrases):
                rows.append({"session": index, "call": call, **row})
        return rows

    started = time.monotonic()
    batches = await asyncio.gather(*(session(i) for i in range(args.sessions)))
    stats.done.set()
    rows = [row for batch in batches for row in batch]
    report = {
        "sessions": args.sessions,
        "calls_per_session": args.calls,
        "seconds": round(time.monotonic() - started),
        "summary": summary(rows, stats),
        "turns": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
