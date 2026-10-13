"""Подменяемые границы транспорта и общего бюджета запросов."""

from datetime import date
from typing import Protocol

from app.mis.models import Client, Doctor, Visit, VisitChange, VisitCreate, VisitStatus


class MisError(Exception):
    """Достоверный отказ без тела HTTP и ПДн; неопределённая запись — MisOutcomeUnknown."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class BudgetExhausted(MisError):
    """Локальный бюджет отказал до отправки HTTP; внешняя операция не начиналась."""


class MisUnavailable(MisError):
    pass


class MisOutcomeUnknown(MisError):
    """Запись могла произойти: повторять изменяющий запрос нельзя."""


class RequestBudget(Protocol):
    async def reserve(self) -> None: ...
    async def defer(self, seconds: int) -> None: ...


class MedicalSystem(Protocol):
    async def doctors(
        self, specialty: str | None = None, *, fresh: bool = False
    ) -> list[Doctor]: ...
    async def clients(self, last_name: str, first_name: str, birth_date: date) -> list[Client]: ...
    async def client(self, client_id: int) -> Client: ...
    async def visits(
        self,
        *,
        client_id: int | None = None,
        doctor_id: int | None = None,
        status: VisitStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[Visit]: ...
    async def create_visit(self, request: VisitCreate) -> Visit: ...
    async def change_visit(self, visit_id: int, request: VisitChange) -> Visit: ...
