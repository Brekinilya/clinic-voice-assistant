"""Текстовый автономный стенд backend с явными командами, без NLU и внешней МИС."""

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

import httpx

from app.booking.memory import MemoryBookingStore
from app.booking.models import ActionState, Approval, BookingError, Slot
from app.booking.service import BookingService
from app.clock import CLINIC_TIMEZONE
from app.mis.emulator import MisEmulator
from app.mis.http import HttpMedicalSystem
from app.mis.models import Client, Doctor, format_datetime, parse_date
from app.mis.ports import MisError


class DemoClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 14, 9, tzinfo=CLINIC_TIMEZONE)

    def now(self) -> datetime:
        return self.value


class DemoSchedule:
    """Вымышленные расписание и допуск; они не переносятся в режим hackathon."""

    def __init__(self, now: datetime) -> None:
        self._slots = tuple(
            Slot(
                doctor_id=1,
                specialty="терапевт",
                scheduled_at=now.replace(hour=10, minute=0, second=0, microsecond=0)
                + timedelta(days=day),
            )
            for day in range(1, 5)
        )

    def slots(self) -> tuple[Slot, ...]:
        return self._slots

    def eligible(self, client_id: int, slot: Slot) -> bool:
        return client_id in {1, 2} and slot in self._slots


class EmulatorBudget:
    """Запросы к MockTransport не расходуют бюджет настоящего стенда."""

    async def reserve(self) -> None:
        pass

    async def defer(self, seconds: int) -> None:
        pass


def demo_emulator(clock: DemoClock) -> MisEmulator:
    clients = [
        Client(
            id=i,
            last_name="Тестов",
            first_name=name,
            middle_name="Примерович",
            birth_date=date(1990, 1, i),
            comment=None,
            created_at=clock.now().replace(second=0, microsecond=0),
        )
        for i, name in ((1, "Александр"), (2, "Иван"))
    ]
    return MisEmulator(
        clock,
        [Doctor(id=1, full_name="Образцова Анна Тестовна", specialty="терапевт", is_active=True)],
        {"demo": clients},
    )


class DemoSession:
    def __init__(self, service: BookingService, emulator: MisEmulator) -> None:
        self.service = service
        self.emulator = emulator
        self.client_id: int | None = None
        self.options = ()
        self.action = None
        self.turn = 0
        self.call_id = "offline-demo"

    async def turn_text(self, text: str) -> list[str]:
        self.turn += 1
        command, _, argument = text.strip().partition(" ")
        try:
            if command == "пациент":
                if self.action is not None:
                    raise BookingError("PATIENT_ALREADY_SELECTED")
                fields = [v.strip() for v in argument.split(";")]
                if len(fields) != 4:
                    raise ValueError("нужны фамилия; имя; отчество; ДД.ММ.ГГГГ")
                client = await self.service.identify(
                    fields[0], fields[1], fields[2] or None, parse_date(fields[3])
                )
                self.client_id, self.options = client.id, ()
                return ["Пациент найден. Укажите специальность командой «варианты терапевт»."]
            if command == "варианты" and self.client_id is not None:
                self.options = await self.service.options(self.client_id, argument)
                return [
                    f"{i}. {format_datetime(s.scheduled_at)}, врач Образцова Анна Тестовна."
                    for i, s in enumerate(self.options, 1)
                ] or ["Доступных вариантов нет."]
            if command == "выбрать" and self.client_id is not None:
                index = int(argument)
                if not 1 <= index <= len(self.options):
                    raise ValueError("неверный номер варианта")
                self.action = await self.service.propose(
                    self.call_id, self.client_id, self.options[index - 1]
                )
                return [
                    f"Бронь: {format_datetime(self.action.slot.scheduled_at)}, врач Образцова Анна Тестовна.",
                    f"Для подтверждения: да {self.action.action_id} {self.action.revision}",
                ]
            if command == "да" and self.action is not None:
                action_id, revision = argument.split()
                # Только автономный командный стенд. Решение реальной safety-системы ещё не подключено.
                approval = Approval(
                    call_id=self.call_id,
                    action_id=action_id,
                    revision=int(revision),
                    turn_id=self.action.confirmed_turn or self.turn,
                    is_final=True,
                    permitted=True,
                )
                self.action = await self.service.confirm(approval)
                return [self._result()]
            if command == "нет" and self.action is not None:
                await self.service.store.cancel(self.action.action_id, self.call_id)
                self.action, self.options = None, ()
                return ["Бронь отменена. Можно выбрать другой вариант."]
            if command == "сверить" and self.action is not None:
                self.action = await self.service.reconcile(self.action.action_id)
                return [self._result()]
            if command == "потерять-ответ":
                self.emulator.next_failure = "lost_response"
                return ["Включена учебная потеря следующего ответа о создании записи."]
            return [
                "Команда не распознана. Доступны: пациент, варианты, выбрать, да, нет, сверить, потерять-ответ."
            ]
        except (BookingError, MisError, ValueError) as error:
            return [f"Действие остановлено: {error}."]

    def _result(self) -> str:
        if self.action.state in {ActionState.BOOKED, ActionState.ARCHIVED}:
            return f"Запись подтверждена. Номер: {self.action.visit_id}."
        if self.action.state in {ActionState.RECONCILING, ActionState.COMMITTING}:
            return "Результат уточняется. Повторная запись не отправляется. Используйте команду «сверить»."
        return "МИС отклонила запись. Выберите новый вариант."


async def run(scripted: bool) -> None:
    clock = DemoClock()
    emulator = demo_emulator(clock)
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/api/v1/",
        headers={"Authorization": "Bearer demo"},
        transport=emulator.transport(),
    ) as client:
        service = BookingService(
            HttpMedicalSystem(client, EmulatorBudget()),
            MemoryBookingStore(),
            DemoSchedule(clock.now()),
            clock,
        )
        session = DemoSession(service, emulator)
        print(
            "Автономный стенд: вымышленные данные, память процесса, явные команды. Для выхода: выход."
        )
        if scripted:
            commands = [
                "пациент Тестов; Александр; Примерович; 01.01.1990",
                "варианты терапевт",
                "выбрать 1",
                "потерять-ответ",
            ]
            for command in commands:
                print(f"> {command}")
                print("\n".join(await session.turn_text(command)))
            command = f"да {session.action.action_id} {session.action.revision}"
            for command in (command, command, "сверить"):
                print(f"> {command}")
                print("\n".join(await session.turn_text(command)))
            print(f"Создано записей в эмуляторе: {emulator.write_count}")
        else:
            while True:
                try:
                    command = input("> ")
                except EOFError:
                    break
                if command.strip() == "выход":
                    break
                print("\n".join(await session.turn_text(command)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Автономный текстовый стенд backend")
    parser.add_argument(
        "--scripted", action="store_true", help="проиграть потерю ответа и повтор подтверждения"
    )
    asyncio.run(run(parser.parse_args().scripted))
