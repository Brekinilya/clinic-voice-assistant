"""Подменяемые границы транспорта и общего бюджета запросов."""

from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Protocol

from app.mis.models import (
    Client,
    ClientComment,
    Doctor,
    Visit,
    VisitChange,
    VisitCreate,
    VisitStatus,
)


class MisError(Exception):
    """Достоверный отказ без тела HTTP и ПДн; неопределённая запись — MisOutcomeUnknown."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class BudgetExhausted(MisError):
    """Локальный бюджет отказал до отправки HTTP; внешняя операция не начиналась."""


class BudgetCancelled(BudgetExhausted):
    """Ожидание бюджета отменено новой речью или отбоем; HTTP не отправлялся."""


class MisUnavailable(MisError):
    pass


class MisOutcomeUnknown(MisError):
    """Запись могла произойти: повторять изменяющий запрос нельзя."""


class RequestBudget(Protocol):
    """Приоритет, срок ожидания и отмену берёт из app.mis.budget.demand."""

    async def reserve(self, cost: int = 1) -> list | None: ...
    async def release(self, ids: list) -> None: ...
    async def touch(self, place: object) -> float: ...
    async def defer(self, seconds: int) -> None: ...


class MedicalSystem(Protocol):
    def clear_call_cache(self, call_id: str) -> None: ...
    def prepaid(self, cost: int, *, keep_for_write: int = 0) -> AbstractAsyncContextManager: ...
    def known_doctors(self) -> list[Doctor] | None: ...
    def has_client(self, client_id: int) -> bool: ...
    async def doctors(
        self, specialty: str | None = None, *, fresh: bool = False
    ) -> list[Doctor]: ...
    async def clients(self, last_name: str, first_name: str, birth_date: date) -> list[Client]: ...
    async def client(self, client_id: int, *, fresh: bool = False) -> Client: ...
    async def visit(self, visit_id: int) -> Visit: ...
    async def visits(
        self,
        *,
        client_id: int | None = None,
        doctor_id: int | None = None,
        status: VisitStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        call_id: str | None = None,
        fresh: bool = False,
    ) -> list[Visit]: ...
    async def create_visit(self, request: VisitCreate) -> Visit: ...
    async def change_visit(self, visit_id: int, request: VisitChange) -> Visit: ...
    async def update_client_comment(self, client_id: int, request: ClientComment) -> Client: ...
