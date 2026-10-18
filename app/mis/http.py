"""HTTP-адаптер: POST/PATCH никогда не повторяются, GET — только после 429 и в пределах срока.

Каждый запрос проходит общий бюджет. Постраничное чтение, прерванное лимитом,
тот же владелец продолжает с прерванной страницы, пока прочитанное актуально.
"""

import hashlib
import math
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from time import monotonic
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.diagnostics.recorder import trace_mis_request
from app.mis.budget import current_demand, immediate
from app.mis.models import (
    Client,
    ClientComment,
    Doctor,
    Visit,
    VisitChange,
    VisitCreate,
    VisitStatus,
    format_date,
    name_key,
)
from app.mis.ports import (
    BudgetExhausted,
    MisError,
    MisOutcomeUnknown,
    MisUnavailable,
    RequestBudget,
)
from app.settings import AppMode, Settings

T = TypeVar("T", bound=BaseModel)

# T00: страница стенда — не больше 50 объектов.
PAGE = 50
# Ограничение защищает от бесконечной пагинации при повреждённом ответе.
MAX_PAGES = 200
# Сколько секунд прочитанное считается актуальным, по типу данных.
FRESHNESS_S = {
    # Справочник меняется редко; активность врача перед записью читается заново.
    "doctors": 600,
    "clients": 300,
    # Занятость и записи — только в пределах звонка; перед записью читаются заново.
    "visits": 90,
}
RETRY_AFTER_MAX_S = 600


@dataclass
class _Prepaid:
    ids: list
    keep_for_write: int

    def take(self, write: bool) -> tuple[bool, object]:
        if not self.ids or (not write and len(self.ids) <= self.keep_for_write):
            return False, None
        if write:
            # Запись ушла: оставшиеся места — для сверки её результата.
            self.keep_for_write = 0
        return True, self.ids.pop(0)


@dataclass
class _Partial:
    items: list
    seen: dict[int, BaseModel]
    offset: int
    started: float
    resumed: bool = field(default=False)


_prepaid: ContextVar[_Prepaid | None] = ContextVar("mis_prepaid", default=None)


class HttpMedicalSystem:
    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: RequestBudget,
        *,
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        # Клиент принадлежит вызвавшему коду; закрывается его контекстным менеджером.
        self._client = client
        self._budget = budget
        self._elapsed = elapsed
        self._doctor_cache: dict[tuple[str, str | None], tuple[float, tuple[Doctor, ...]]] = {}
        self._directory: dict[str, tuple[Doctor, ...]] = {}
        self._client_cache: dict[tuple[str, int], tuple[float, Client]] = {}
        self._visit_cache: dict[
            tuple[str, str, int | None, int | None, VisitStatus | None, date | None, date | None],
            tuple[float, tuple[Visit, ...]],
        ] = {}
        self._partial: dict[tuple, _Partial] = {}

    def _scope(self) -> str:
        return hashlib.sha256(self._client.headers.get("Authorization", "").encode()).hexdigest()

    def clear_call_cache(self, call_id: str) -> None:
        for key in list(self._visit_cache):
            if key[1] == call_id:
                del self._visit_cache[key]
        for key in list(self._partial):
            if key[1] == call_id:
                del self._partial[key]

    def _invalidate_visits(self) -> None:
        scope = self._scope()
        for key in list(self._visit_cache):
            if key[0] == scope:
                del self._visit_cache[key]
        for key in list(self._partial):
            if key[0] == scope and key[2] == "visits":
                del self._partial[key]

    def _cache(self, cache: dict, key: tuple, value: object, ttl: int) -> None:
        now = self._elapsed()
        for expired in [k for k, (until, _) in cache.items() if until <= now]:
            del cache[expired]
        cache.pop(key, None)
        while len(cache) >= 128:
            del cache[next(iter(cache))]
        cache[key] = (now + ttl, value)

    @staticmethod
    def live_client(settings: Settings) -> httpx.AsyncClient:
        if settings.app_mode != AppMode.HACKATHON:
            raise ValueError("внешняя МИС доступна только в режиме hackathon")
        return httpx.AsyncClient(
            base_url=f"{str(settings.mis_base_url).rstrip('/')}/",
            headers={"Authorization": f"Bearer {settings.mis_token.get_secret_value()}"},
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            trust_env=False,
        )

    @asynccontextmanager
    async def prepaid(self, cost: int, *, keep_for_write: int = 0):
        """Места для свежей проверки и записи занимаются сразу: между ними нет ожидания.

        Чтения берут предоплату, пока остаётся keep_for_write мест для записи; после записи
        остаток идёт на сверку. Неиспользованные места возвращаются: HTTP по ним не было.
        """
        granted = await self._budget.reserve(cost)
        lease = _Prepaid(list(granted) if granted is not None else [None] * cost, keep_for_write)
        token = _prepaid.set(lease)
        try:
            yield
        finally:
            _prepaid.reset(token)
            unused = [i for i in lease.ids if i is not None]
            if unused:
                try:
                    await self._budget.release(unused)
                except Exception:
                    # Не вернули место — окно просто будет считать его занятым до истечения.
                    pass

    @trace_mis_request
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        from app.diagnostics.recorder import emit, resource_path

        write = method != "GET"
        retried = False
        while True:
            lease = _prepaid.get()
            taken, place = (False, None) if lease is None else lease.take(write)
            if taken and place is not None:
                # Окно считает момент отправки, а не бронирования места: иначе пачка
                # предоплаченных запросов могла бы превысить внешний лимит стенда.
                # Пауза после 429 действует и на предоплату: такой запрос не уходит.
                paused = await self._budget.touch(place)
                if paused:
                    raise BudgetExhausted("RATE_LIMIT", retry_after=max(1, math.ceil(paused)))
            if not taken:
                if write:
                    # Запись без предоплаты не ждёт окна: журнал уже зафиксировал согласие.
                    with immediate():
                        await self._budget.reserve()
                else:
                    await self._budget.reserve()
            try:
                if write:
                    # Проверяем ещё раз после ожидания бюджета, прямо перед отправкой.
                    # Фоновая сверка вообще не имеет права посылать POST/PATCH.
                    from app.recovery.ownership import before_mutation

                    await before_mutation()
                emit("mis.sent", method=method, resource=resource_path(path))
                response = await self._client.request(method, path, **kwargs)
                emit("mis.response", status=response.status_code)
            except httpx.TransportError:
                error = MisOutcomeUnknown if write else MisUnavailable
                raise error("TRANSPORT_FAILURE") from None
            if response.status_code != 429:
                break
            raw = response.headers.get("Retry-After", "60")
            delay = int(raw) if raw.isascii() and raw.isdigit() else 60
            delay = min(RETRY_AFTER_MAX_S, max(1, delay))
            # Стенд отказал до обработки; окно закрываем для всех процессов команды.
            await self._budget.defer(delay)
            until = current_demand().until
            if write or retried or until is None:
                raise MisError("RATE_LIMIT", retry_after=delay)
            # Чтение повторяем один раз: бюджет сам дождётся конца паузы или откажет по сроку.
            retried = True
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            error = MisOutcomeUnknown if write else MisUnavailable
            raise error("SERVER_FAILURE")
        if response.status_code not in ({200} if method == "GET" else {200, 201}):
            codes = {400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 404: "NOT_FOUND", 409: "CONFLICT"}
            if response.status_code not in codes and write:
                raise MisOutcomeUnknown("UNEXPECTED_STATUS")
            raise MisError(codes.get(response.status_code, "UNEXPECTED_STATUS"))
        return response

    @staticmethod
    def _decode(response: httpx.Response, model: type[T], *, write: bool = False) -> T:
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError):
            from app.diagnostics.recorder import emit

            emit(
                "mis.error",
                code="INVALID_RESPONSE",
                error="MisOutcomeUnknown" if write else "MisUnavailable",
            )
            error = MisOutcomeUnknown if write else MisUnavailable
            raise error("INVALID_RESPONSE") from None

    def _resume(self, key: tuple, ttl: int) -> _Partial | None:
        now = self._elapsed()
        for stale in [k for k, p in self._partial.items() if p.started + FRESHNESS_S[k[2]] <= now]:
            del self._partial[stale]
        partial = self._partial.pop(key, None)
        if partial is None or partial.started + ttl <= now:
            return None
        partial.resumed = True
        return partial

    async def _list(self, path: str, model: type[T], params: dict, *, resume=True) -> list[T]:
        owner = current_demand().owner
        key = (self._scope(), owner, path, tuple(sorted(params.items())))
        partial = self._resume(key, FRESHNESS_S[path]) if resume and owner is not None else None
        if partial is None:
            partial = _Partial(items=[], seen={}, offset=0, started=self._elapsed())
        while partial.offset < PAGE * MAX_PAGES:
            try:
                response = await self._request(
                    "GET", path, params={**params, "limit": PAGE, "offset": partial.offset}
                )
            except MisError:
                # Прочитанные страницы не выбрасываем: следующая попытка продолжит отсюда.
                if partial.offset and owner is not None and resume:
                    while len(self._partial) >= 16:
                        del self._partial[next(iter(self._partial))]
                    partial.resumed = False
                    self._partial[key] = partial
                raise
            try:
                payload = response.json()
                if not isinstance(payload, list) or len(payload) > PAGE:
                    raise ValueError("некорректная страница")
                page = [model.model_validate(item) for item in payload]
                fresh = []
                for item in page:
                    known = partial.seen.get(item.id)
                    if known is None:
                        partial.seen[item.id] = item
                        fresh.append(item)
                    # После паузы новая запись перед смещением сдвигает страницу на ту же
                    # карточку. Неизменный повтор пропускаем, иначе ответ некорректен.
                    elif not partial.resumed or known != item:
                        raise ValueError("повтор карточки между страницами")
            except (ValidationError, ValueError):
                raise MisUnavailable("INVALID_RESPONSE") from None
            partial.resumed = False
            partial.items.extend(fresh)
            partial.offset += PAGE
            if len(page) < PAGE:
                return partial.items
        raise MisUnavailable("PAGINATION_LIMIT")

    def known_doctors(self) -> list[Doctor] | None:
        """Последний прочитанный справочник без обращения к сети: для понимания, не для записи."""
        known = self._directory.get(self._scope())
        return None if known is None else list(known)

    async def doctors(self, specialty: str | None = None, *, fresh: bool = False) -> list[Doctor]:
        key = (self._scope(), specialty)
        cached = self._doctor_cache.get(key)
        if not fresh and cached is not None and cached[0] > self._elapsed():
            return list(cached[1])
        all_doctors = self._doctor_cache.get((key[0], None))
        if not fresh and all_doctors is not None and all_doctors[0] > self._elapsed():
            return [d for d in all_doctors[1] if specialty is None or d.specialty == specialty]
        result = await self._list(
            "doctors",
            Doctor,
            {} if specialty is None else {"specialty": specialty},
            resume=not fresh,
        )
        self._cache(self._doctor_cache, key, tuple(result), FRESHNESS_S["doctors"])
        if specialty is None:
            self._directory[key[0]] = tuple(result)
        return result

    async def clients(self, last_name: str, first_name: str, birth_date: date) -> list[Client]:
        expected_last, expected_first = name_key(last_name), name_key(first_name)
        # Фильтры ФИО стенда чувствительны к регистру. Ищем по дате рождения,
        # затем точно сравниваем нормализованные имена, не перебирая варианты HTTP.
        result = await self._list(
            "clients",
            Client,
            {"birth_date": format_date(birth_date)},
        )
        result = [
            c
            for c in result
            if c.birth_date == birth_date
            and name_key(c.last_name) == expected_last
            and name_key(c.first_name) == expected_first
        ]
        for item in result:
            self._cache(self._client_cache, (self._scope(), item.id), item, FRESHNESS_S["clients"])
        return result

    def has_client(self, client_id: int) -> bool:
        """Карточка уже прочитана и актуальна: свежая проверка обойдётся без её запроса."""
        cached = self._client_cache.get((self._scope(), client_id))
        return cached is not None and cached[0] > self._elapsed()

    async def client(self, client_id: int, *, fresh: bool = False) -> Client:
        self._id(client_id)
        key = (self._scope(), client_id)
        cached = self._client_cache.get(key)
        # Перед изменением комментария читаем его заново: чужую правку нельзя затереть
        if not fresh and cached is not None and cached[0] > self._elapsed():
            return cached[1]
        result = self._decode(await self._request("GET", f"clients/{client_id}"), Client)
        if result.id != client_id:
            raise MisUnavailable("INVALID_RESPONSE")
        self._cache(self._client_cache, key, result, FRESHNESS_S["clients"])
        return result

    async def visit(self, visit_id: int) -> Visit:
        self._id(visit_id)
        result = self._decode(await self._request("GET", f"visits/{visit_id}"), Visit)
        if result.id != visit_id:
            raise MisUnavailable("INVALID_RESPONSE")
        return result

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
    ) -> list[Visit]:
        params = {}
        for key, value in (("client_id", client_id), ("doctor_id", doctor_id)):
            if value is not None:
                self._id(value)
                params[key] = value
        if status is not None:
            params["status"] = VisitStatus(status).value
        for key, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None:
                params[key] = format_date(value)
        # Кэш разрешён только для явного звонка и узкого запроса.
        # Справка С-05 и сверка исхода записи по умолчанию всегда читают МИС.
        cache_key = (self._scope(), call_id, client_id, doctor_id, status, date_from, date_to)
        cacheable = call_id is not None and (client_id is not None or doctor_id is not None)
        now = self._elapsed()
        for key, (until, items) in list(self._visit_cache.items()):
            if until <= now:
                del self._visit_cache[key]
                continue
            if (
                cacheable
                and not fresh
                and key[:5] == cache_key[:5]
                and (key[5] is None or (date_from is not None and key[5] <= date_from))
                and (key[6] is None or (date_to is not None and key[6] >= date_to))
            ):
                return [
                    v
                    for v in items
                    if (date_from is None or v.scheduled_at.date() >= date_from)
                    and (date_to is None or v.scheduled_at.date() <= date_to)
                ]
        # Свежая проверка занятости не продолжает старое чтение: пропуск отменённой
        # между страницами записи сдвинул бы смещение и скрыл занятый слот.
        result = await self._list("visits", Visit, params, resume=not fresh)
        # Большие ответы остаются свежими; объём памяти ограничен 128 снимками по 200 строк.
        if cacheable and len(result) <= 200:
            self._cache(self._visit_cache, cache_key, tuple(result), FRESHNESS_S["visits"])
        return result

    async def create_visit(self, request: VisitCreate) -> Visit:
        # Даже потеря ответа могла изменить МИС; старые снимки больше не используем.
        self._invalidate_visits()
        try:
            response = await self._request("POST", "visits", json=request.model_dump(mode="json"))
            return self._decode(response, Visit, write=True)
        finally:
            # Убираем и снимки, прочитанные параллельно с изменением.
            self._invalidate_visits()

    async def change_visit(self, visit_id: int, request: VisitChange) -> Visit:
        self._id(visit_id)
        self._invalidate_visits()
        try:
            response = await self._request("PATCH", f"visits/{visit_id}", json=request.body())
            return self._decode(response, Visit, write=True)
        finally:
            self._invalidate_visits()

    async def update_client_comment(self, client_id: int, request: ClientComment) -> Client:
        self._id(client_id)
        body = request.body()
        self._invalidate_clients(client_id)
        try:
            response = await self._request("PATCH", f"clients/{client_id}", json=body)
            result = self._decode(response, Client, write=True)
        finally:
            self._invalidate_clients(client_id)
        if result.id != client_id:
            raise MisOutcomeUnknown("INVALID_RESPONSE")
        return result

    def _invalidate_clients(self, client_id: int) -> None:
        for key in [k for k in self._client_cache if k[1] == client_id]:
            del self._client_cache[key]

    @staticmethod
    def _id(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 2147483647:
            raise ValueError("неверный идентификатор МИС")
