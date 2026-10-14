"""HTTP-адаптер: ни GET, ни POST/PATCH автоматически не повторяются."""

import hashlib
from collections.abc import Callable
from datetime import date
from time import monotonic
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.mis.models import (
    Client,
    Doctor,
    Visit,
    VisitChange,
    VisitCreate,
    VisitStatus,
    format_date,
    name_key,
)
from app.mis.ports import MisError, MisOutcomeUnknown, MisUnavailable, RequestBudget
from app.settings import AppMode, Settings

T = TypeVar("T", bound=BaseModel)


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
        self._client_cache: dict[tuple[str, int], tuple[float, Client]] = {}
        self._visit_cache: dict[
            tuple[str, str, int | None, int | None, VisitStatus | None, date | None, date | None],
            tuple[float, tuple[Visit, ...]],
        ] = {}

    def _scope(self) -> str:
        return hashlib.sha256(self._client.headers.get("Authorization", "").encode()).hexdigest()

    def clear_call_cache(self, call_id: str) -> None:
        for key in list(self._visit_cache):
            if key[1] == call_id:
                del self._visit_cache[key]

    def _invalidate_visits(self) -> None:
        scope = self._scope()
        for key in list(self._visit_cache):
            if key[0] == scope:
                del self._visit_cache[key]

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

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._budget.reserve()
        write = method != "GET"
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TransportError:
            error = MisOutcomeUnknown if write else MisUnavailable
            raise error("TRANSPORT_FAILURE") from None
        if response.status_code == 429:
            raw = response.headers.get("Retry-After", "60")
            delay = int(raw) if raw.isascii() and raw.isdigit() else 60
            delay = max(1, delay)
            await self._budget.defer(delay)
            raise MisError("RATE_LIMIT", retry_after=delay)
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
            error = MisOutcomeUnknown if write else MisUnavailable
            raise error("INVALID_RESPONSE") from None

    async def _list(self, path: str, model: type[T], params: dict) -> list[T]:
        items: list[T] = []
        seen: set[int] = set()
        # Ограничение защищает от бесконечной пагинации при повреждённом ответе.
        for offset in range(0, 10000, 50):
            response = await self._request(
                "GET", path, params={**params, "limit": 50, "offset": offset}
            )
            try:
                payload = response.json()
                if not isinstance(payload, list) or len(payload) > 50:
                    raise ValueError("некорректная страница")
                page = [model.model_validate(item) for item in payload]
                for item in page:
                    if item.id in seen:
                        raise ValueError("повтор карточки между страницами")
                    seen.add(item.id)
            except (ValidationError, ValueError):
                raise MisUnavailable("INVALID_RESPONSE") from None
            items.extend(page)
            if len(page) < 50:
                return items
        raise MisUnavailable("PAGINATION_LIMIT")

    async def doctors(self, specialty: str | None = None, *, fresh: bool = False) -> list[Doctor]:
        key = (self._scope(), specialty)
        cached = self._doctor_cache.get(key)
        if not fresh and cached is not None and cached[0] > self._elapsed():
            return list(cached[1])
        all_doctors = self._doctor_cache.get((key[0], None))
        if not fresh and all_doctors is not None and all_doctors[0] > self._elapsed():
            return [d for d in all_doctors[1] if specialty is None or d.specialty == specialty]
        result = await self._list(
            "doctors", Doctor, {} if specialty is None else {"specialty": specialty}
        )
        self._cache(self._doctor_cache, key, tuple(result), 30)
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
            self._cache(self._client_cache, (self._scope(), item.id), item, 300)
        return result

    async def client(self, client_id: int) -> Client:
        self._id(client_id)
        key = (self._scope(), client_id)
        cached = self._client_cache.get(key)
        if cached is not None and cached[0] > self._elapsed():
            return cached[1]
        result = self._decode(await self._request("GET", f"clients/{client_id}"), Client)
        if result.id != client_id:
            raise MisUnavailable("INVALID_RESPONSE")
        self._cache(self._client_cache, key, result, 300)
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
        result = await self._list("visits", Visit, params)
        # Большие ответы остаются свежими; объём памяти ограничен 128 снимками по 200 строк.
        if cacheable and len(result) <= 200:
            self._cache(self._visit_cache, cache_key, tuple(result), 90)
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

    @staticmethod
    def _id(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 2147483647:
            raise ValueError("неверный идентификатор МИС")
