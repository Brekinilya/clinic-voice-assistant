"""T05: фактические запросы к МИС и время завершения сценариев С-01/С-03/С-04/С-05/С-10.

Сценарии идут через настоящий текстовый API и HTTP-адаптер против эмулятора стенда.
Время виртуальное: реплика пациента занимает THINK_S секунд, ожидание бюджета продвигает
те же часы. Бюджет — общий алгоритм app.mis.budget (лимит 8 в минуту, как PostgresBudget);
разделение между процессами проверено отдельно, tests/test_mis_budget_postgres.py.

    python bench/mis/budget_table.py            # таблица в Markdown
"""

import asyncio
import contextvars
import heapq
import itertools
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "integration")]

import fixture_bridge  # noqa: E402

from app.mis.budget import MemoryBudget, Priority, demand  # noqa: E402
from app.mis.models import Doctor, Visit, VisitStatus, parse_date  # noqa: E402
from app.mis.ports import BudgetExhausted  # noqa: E402

SCENARIOS = ROOT / "tests" / "scenarios" / "main"
# Реплика пациента вместе с ответом ассистента.
THINK_S = 6
actor = contextvars.ContextVar("actor", default="?")


class VirtualTime:
    """Дискретные события: часы идут к ближайшему моменту, когда все участники ждут."""

    def __init__(self, clock):
        self.clock = clock
        self.queue = []
        self.order = itertools.count()
        self.blocked = 0
        self.waited = Counter()

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(
            self.queue, (self.clock.now() + timedelta(seconds=seconds), next(self.order), future)
        )
        self.blocked += 1
        try:
            await future
        finally:
            self.blocked -= 1

    async def budget_sleep(self, seconds):
        self.waited[actor.get()] += seconds
        await self.sleep(seconds)

    async def run(self, actors):
        tasks = [asyncio.create_task(self._named(name, body)) for name, body in actors]
        while not all(t.done() for t in tasks):
            alive = sum(not t.done() for t in tasks)
            for _ in range(10000):
                if self.blocked >= alive or all(t.done() for t in tasks):
                    break
                await asyncio.sleep(0)
                alive = sum(not t.done() for t in tasks)
            if all(t.done() for t in tasks):
                break
            moment, _, future = heapq.heappop(self.queue)
            self.clock.value = max(self.clock.value, moment)
            future.set_result(None)
            await asyncio.sleep(0)
        return [t.result() for t in tasks]

    @staticmethod
    async def _named(name, body):
        actor.set(name)
        return await body


def heavy(emulator, clock):
    """Профиль нагрузки T05: 85 врачей, больше 50 пациентов и записей на один день."""
    base = max(emulator.doctors) + 1
    for i in range(base, base + 85 - len(emulator.doctors)):
        emulator.doctors[i] = Doctor(
            id=i, full_name=f"Дополнительнова{i} Анна Сергеевна", specialty="хирург", is_active=True
        )
    clients = emulator.clients["demo"]
    template = next(iter(clients.values()))
    for i in range(1000, 1060):
        # Однофамильцы по дате рождения каждого учебного пациента: поиск занимает 2 страницы.
        for source in list(clients.values())[:4]:
            clients[i * 10 + source.id] = template.model_copy(
                update={
                    "id": i * 10 + source.id,
                    "last_name": f"Посторонний{i}",
                    "birth_date": source.birth_date,
                }
            )
    other = max(c for c in clients)
    records = emulator.records["demo"]
    first = max(records, default=0) + 1
    # По 60 чужих записей на каждый день недели у каждого учебного врача: чтение дня — 2 страницы.
    for doctor_id in list(emulator.doctors)[:9]:
        for offset in range(9):
            day = parse_date("01.10.2026") + timedelta(days=offset)
            at = clock.now().replace(year=day.year, month=day.month, day=day.day, hour=7, minute=0)
            for n in range(60):
                records[first] = Visit(
                    id=first,
                    client_id=other,
                    doctor=emulator.doctors[doctor_id],
                    # Раньше рабочих слотов: занятость читается, но слоты не закрывает.
                    scheduled_at=at - timedelta(minutes=n + 1),
                    status=VisitStatus.PLANNED,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
                first += 1


def steps(name):
    case = yaml.safe_load((SCENARIOS / name).read_text(encoding="utf-8"))
    texts = [s["patient"] for s in case["steps"] if "patient" in s]
    # Прощание после результата МИС не нужно.
    return case["setup"], [t for t in texts if t != "Нет, спасибо."]


async def call(vt, client, texts, call_id, done_codes):
    started = vt.clock.now()
    number, codes, waits = 0, [], 0
    script, rounds, index = list(texts), 0, 0
    while index < len(script):
        text = script[index]
        index += 1
        while True:
            await vt.sleep(THINK_S)
            number += 1
            result = await fixture_bridge.turn(client, number, text, call_id=call_id)
            codes.append(result["code"])
            if result["code"] != "WAIT_MIS":
                break
            # Пациента попросили подождать — ждёт и делает то, о чём попросили.
            waits += 1
            left = datetime.fromisoformat(result["retry_at"]) - vt.clock.now()
            await vt.sleep(max(0, left.total_seconds()))
            said = " ".join(result["utterances"])
            text = (
                "Да."
                if "подтвердите ещё раз" in said
                else "Ещё раз."
                if "«ещё раз»" in said
                else text
            )
        if result["code"] in done_codes or result.get("transfer"):
            break
        if index == len(script) and rounds < 4:
            # Сценарий кончился раньше результата: за долгое ожидание бронь или согласие
            # устарели. Человек отвечает на вопрос: подтверждает или снова выбирает.
            if (result["code"] or "").endswith("CONFIRMATION_REQUIRED"):
                script.append("Да.")
            elif result["options"]:
                script.append(texts[-2])
            rounds += 1
    return {
        "code": result["code"],
        "seconds": (vt.clock.now() - started).total_seconds(),
        "turns": number,
        "waits": waits,
    }


async def background(vt, mis, reminder_day):
    """Каталог раз в 20 с и выборка напоминаний на завтра — фоновый приоритет (T11 — позже)."""
    started, postponed = vt.clock.now(), 0
    for _ in range(3):
        while True:
            try:
                with demand(
                    Priority.BACKGROUND, until=vt.clock.now() + timedelta(seconds=30), owner="job"
                ):
                    await mis.doctors(fresh=True)
                    await mis.visits(
                        status=VisitStatus.PLANNED, date_from=reminder_day, date_to=reminder_day
                    )
                break
            except BudgetExhausted as error:
                # Фоновая задача уступает разговорам и пробует позже, а не теряет работу.
                postponed += 1
                await vt.sleep(error.retry_after)
        await vt.sleep(20)
    return {
        "code": "DONE",
        "seconds": (vt.clock.now() - started).total_seconds(),
        "turns": 0,
        "waits": postponed,
    }


async def scenario(name, texts_setup, done, *, contention, profile):
    setup, texts = texts_setup
    clock = fixture_bridge.ScenarioClock(setup.get("now", "30.09.2026 10:15"))
    vt = VirtualTime(clock)
    budget = MemoryBudget(clock, sleep=vt.budget_sleep)
    original = fixture_bridge.ScenarioClock
    fixture_bridge.ScenarioClock = lambda value="": clock
    try:
        async with fixture_bridge.pipeline(setup) as (client, service, emulator, *_):
            async with fixture_bridge.pipeline(steps("c01_booking.yaml")[0]) as (
                other,
                other_service,
                other_emulator,
                *_,
            ):
                for item in (service, other_service):
                    item.booking.mis._budget = budget
                if profile == "heavy":
                    heavy(emulator, clock)
                    heavy(other_emulator, clock)
                actors = [("call", call(vt, client, texts, "measured", done))]
                if contention:
                    _, other_texts = steps("c01_booking.yaml")
                    actors.append(("second", call(vt, other, other_texts, "second", {"BOOKED"})))
                    actors.append(
                        ("job", background(vt, other_service.booking.mis, parse_date("01.10.2026")))
                    )
                results = await vt.run(actors)
                log = emulator.log
                merged = sorted(at for at, *_ in emulator.log + other_emulator.log)
    finally:
        fixture_bridge.ScenarioClock = original
    result = results[0]
    counts = Counter(f"{m} {p.split('/')[0]}" for _, m, p, _ in log)
    pages = sum(1 for _, m, p, offset in log if m == "GET" and offset > 0)
    peak = max(
        (sum(1 for u in merged if t <= u < t + timedelta(seconds=60)) for t in merged), default=0
    )
    return {
        "name": name,
        "code": result["code"],
        "requests": len(log),
        "detail": ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items())),
        "extra_pages": pages,
        "seconds": result["seconds"],
        "turns": result["turns"],
        "waits": result["waits"],
        "budget_wait": vt.waited["call"],
        "peak": peak,
        "limited": emulator.limited + other_emulator.limited,
    }


CASES = [
    ("С-01 запись", "c01_booking.yaml", {"BOOKED"}),
    ("С-03 перенос", "c03_reschedule.yaml", {"RESCHEDULED"}),
    ("С-04 отмена", "c04_cancel.yaml", {"CANCELLED"}),
    ("С-05 справка", "c05_my_visit.yaml", {"INFO_GIVEN"}),
]


async def reminder_only(profile):
    """С-10: исходящего звонка ещё нет (T11); фоновая выборка записей на завтра."""
    setup = yaml.safe_load((SCENARIOS / "c10_reminder.yaml").read_text(encoding="utf-8"))["setup"]
    clock = fixture_bridge.ScenarioClock(setup["now"])
    vt = VirtualTime(clock)
    budget = MemoryBudget(clock, sleep=vt.budget_sleep)
    original = fixture_bridge.ScenarioClock
    fixture_bridge.ScenarioClock = lambda value="": clock
    try:
        async with fixture_bridge.pipeline(setup) as (client, service, emulator, *_):
            service.booking.mis._budget = budget
            if profile == "heavy":
                heavy(emulator, clock)
            day = parse_date("01.10.2026")

            async def job():
                started = clock.now()
                with demand(
                    Priority.BACKGROUND,
                    until=clock.now() + timedelta(seconds=120),
                    owner="reminders",
                ):
                    found = await service.booking.mis.visits(
                        status=VisitStatus.PLANNED, date_from=day, date_to=day
                    )
                return {
                    "code": f"{len(found)} записей",
                    "seconds": (clock.now() - started).total_seconds(),
                }

            (result,) = await vt.run([("call", job())])
            log = emulator.log
    finally:
        fixture_bridge.ScenarioClock = original
    counts = Counter(f"{m} {p.split('/')[0]}" for _, m, p, _ in log)
    return {
        "name": "С-10 напоминание (выборка)",
        "code": result["code"],
        "requests": len(log),
        "detail": ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items())),
        "extra_pages": sum(1 for _, m, _, offset in log if offset > 0),
        "seconds": result["seconds"],
        "turns": 0,
        "waits": 0,
        "budget_wait": vt.waited["call"],
        "peak": max(
            (sum(1 for u in log if at <= u[0] < at + timedelta(seconds=60)) for at, *_ in log),
            default=0,
        ),
        "limited": emulator.limited,
    }


async def main():
    rows = []
    for profile in ("base", "heavy"):
        for contention in (False, True):
            for name, path, done in CASES:
                rows.append(
                    (
                        profile,
                        contention,
                        await scenario(
                            name, steps(path), done, contention=contention, profile=profile
                        ),
                    )
                )
            if not contention:
                rows.append((profile, contention, await reminder_only(profile)))
    print(
        "| Профиль | Нагрузка | Сценарий | Итог | Запросов | Из них | Доп. страниц | Реплик | «Подождите» | Ждали бюджет, с | Завершение, с | Пик за 60 с | 429 |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for profile, contention, r in rows:
        print(
            f"| {'базовый' if profile == 'base' else '85 врачей, >50'} | "
            f"{'+ второй звонок и фон' if contention else 'один звонок'} | {r['name']} | {r['code']} | "
            f"{r['requests']} | {r['detail']} | {r['extra_pages']} | {r['turns']} | {r['waits']} | "
            f"{r['budget_wait']:.0f} | {r['seconds']:.0f} | {r['peak']} | {r['limited']} |"
        )


if __name__ == "__main__":
    asyncio.run(main())
