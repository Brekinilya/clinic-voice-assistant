"""Автономный HTTP-стенд только для методов нашего адаптера; реальные данные запрещены."""

import asyncio
import json
import math
from collections import deque
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

import httpx
from pydantic import ValidationError

from app.mis.models import (
    COMMENT_LIMIT,
    ESCALATION_LIMIT,
    Client,
    ClientCreate,
    Doctor,
    Escalation,
    Visit,
    VisitChange,
    VisitCreate,
    VisitStatus,
    normalize_name,
    parse_date,
    valid_text,
)
from app.ports import Clock


class MisEmulator:
    def __init__(
        self,
        clock: Clock,
        doctors: list[Doctor],
        clients: dict[str, list[Client]],
        *,
        rate_limit: int | None = None,
        rate_window_s: float = 60,
    ) -> None:
        self.clock = clock
        # Эмулятор ограничений: как стенд по T00, не больше rate_limit запросов в минуту
        # на токен, сверх — 429 с Retry-After. None — без ограничения.
        self.rate_limit = rate_limit
        self.rate_window_s = rate_window_s
        self.limited = 0
        self._hits: dict[str, deque[datetime]] = {}
        # Журнал принятых запросов без тел: метод, путь и смещение страницы.
        self.log: list[tuple[datetime, str, str, int]] = []
        self.doctors = {item.id: item for item in doctors}
        self.clients = {
            token: {item.id: item for item in items} for token, items in clients.items()
        }
        self.records: dict[str, dict[int, Visit]] = {token: {} for token in clients}
        self.escalations: dict[str, dict[int, Escalation]] = {token: {} for token in clients}
        self.escalation_count = 0
        self.write_count = 0
        self.patch_count = 0
        self.next_failure: str | None = None
        self._lock = asyncio.Lock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def error(status: int) -> httpx.Response:
        codes = {
            400: "НЕВЕРНЫЙ_ЗАПРОС",
            401: "НЕ_АВТОРИЗОВАН",
            404: "НЕ_НАЙДЕНО",
            409: "КОНФЛИКТ_СТАТУСА",
            500: "ВНУТРЕННЯЯ_ОШИБКА",
        }
        return httpx.Response(status, json={"code": codes[status], "message": "Отказ эмулятора"})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        async with self._lock:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if token not in self.clients:
                return self.error(401)
            path = request.url.path.removeprefix("/api/v1").strip("/").split("/")
            query = {
                k: values[-1]
                for k, values in parse_qs(
                    request.url.query.decode(), keep_blank_values=True
                ).items()
            }
            limited = self._limit(token)
            if limited is not None:
                return limited
            offset = int(query["offset"]) if query.get("offset", "").isdigit() else 0
            self.log.append((self.clock.now(), request.method, "/".join(path), offset))
            try:
                if len(path) == 1 and request.method == "GET":
                    return self._list(path[0], token, query)
                if path == ["escalations"] and request.method == "POST":
                    payload = json.loads(request.content)
                    client_id, text = payload.get("client_id"), payload.get("description")
                    # Чужой или несуществующий клиент — 400: это значение поля, а не адрес
                    if (
                        type(client_id) is not int
                        or client_id not in self.clients[token]
                        or not isinstance(text, str)
                        or len(text) > ESCALATION_LIMIT
                        or not valid_text(text)
                    ):
                        return self.error(400)
                    item = Escalation(
                        id=max(self.escalations[token], default=0) + 1,
                        client_id=client_id,
                        description=text,
                        created_at=self.clock.now().replace(second=0, microsecond=0),
                    )
                    self.escalations[token][item.id] = item
                    self.escalation_count += 1
                    failure, self.next_failure = self.next_failure, None
                    if failure == "lost_response":
                        raise httpx.ReadTimeout("ответ потерян", request=request)
                    if failure == "server_error":
                        return self.error(500)
                    return httpx.Response(201, json=item.model_dump(mode="json"))
                if path == ["clients"] and request.method == "POST":
                    data = ClientCreate.model_validate(json.loads(request.content))
                    key = (data.last_name, data.first_name, data.birth_date)
                    for known in self.clients[token].values():
                        if (known.last_name, known.first_name, known.birth_date) == key:
                            return httpx.Response(200, json=known.model_dump(mode="json"))
                    client = Client(
                        id=max(self.clients[token], default=0) + 1,
                        last_name=data.last_name,
                        first_name=data.first_name,
                        middle_name=data.middle_name,
                        birth_date=data.birth_date,
                        comment=data.comment,
                        created_at=self.clock.now().replace(second=0, microsecond=0),
                    )
                    self.clients[token][client.id] = client
                    return httpx.Response(201, json=client.model_dump(mode="json"))
                if path == ["visits"] and request.method == "POST":
                    payload = json.loads(request.content)
                    data = VisitCreate.model_validate(
                        {k: v for k, v in payload.items() if k in VisitCreate.model_fields}
                    )
                    if (
                        data.client_id not in self.clients[token]
                        or data.doctor_id not in self.doctors
                    ):
                        return self.error(400)
                    doctor = self.doctors[data.doctor_id]
                    if not doctor.is_active:
                        return self.error(400)
                    now = self.clock.now().replace(second=0, microsecond=0)
                    visit = Visit(
                        id=max(self.records[token], default=0) + 1,
                        client_id=data.client_id,
                        doctor=doctor,
                        scheduled_at=data.scheduled_at,
                        status=VisitStatus.PLANNED,
                        created_at=now,
                        updated_at=now,
                    )
                    # Намеренно НЕ проверяем занятость: так устроен настоящий стенд.
                    self.records[token][visit.id] = visit
                    self.write_count += 1
                    failure, self.next_failure = self.next_failure, None
                    if failure == "lost_response":
                        raise httpx.ReadTimeout("ответ потерян", request=request)
                    if failure == "server_error":
                        return self.error(500)
                    if failure == "invalid_response":
                        return httpx.Response(201, json={"unexpected": True})
                    return httpx.Response(201, json=visit.model_dump(mode="json"))
                if len(path) == 2 and request.method == "GET":
                    item_id = int(path[1])
                    items = (
                        self.clients[token]
                        if path[0] == "clients"
                        else self.records[token]
                        if path[0] == "visits"
                        else self.escalations[token]
                        if path[0] == "escalations"
                        else {}
                    )
                    if item_id not in items:
                        return self.error(404)
                    return httpx.Response(200, json=items[item_id].model_dump(mode="json"))
                if len(path) == 2 and path[0] == "clients" and request.method == "PATCH":
                    item_id = int(path[1])
                    if item_id not in self.clients[token]:
                        return self.error(404)
                    payload = json.loads(request.content)
                    # Как стенд: ФИО и дата рождения — ключ пациента, их не меняют
                    if not payload or set(payload) - {"middle_name", "comment"}:
                        return self.error(400)
                    comment = payload.get("comment", self.clients[token][item_id].comment)
                    if comment is not None and (
                        not isinstance(comment, str) or len(comment) > COMMENT_LIMIT
                    ):
                        return self.error(400)
                    client = self.clients[token][item_id].model_copy(
                        update={k: v for k, v in payload.items() if k in ("middle_name", "comment")}
                    )
                    self.clients[token][item_id] = client
                    self.patch_count += 1
                    failure, self.next_failure = self.next_failure, None
                    if failure == "lost_response":
                        raise httpx.ReadTimeout("ответ потерян", request=request)
                    if failure == "server_error":
                        return self.error(500)
                    return httpx.Response(200, json=client.model_dump(mode="json"))
                if len(path) == 2 and path[0] == "visits" and request.method == "PATCH":
                    item_id = int(path[1])
                    if item_id not in self.records[token]:
                        return self.error(404)
                    payload = json.loads(request.content)
                    if "client_id" in payload:
                        return self.error(400)
                    data = VisitChange.model_validate(
                        {k: v for k, v in payload.items() if k in VisitChange.model_fields}
                    )
                    body = data.model_dump(exclude_unset=True)
                    if any(v is None for v in body.values()):
                        return self.error(400)
                    visit = self.records[token][item_id]
                    if not body or (
                        body == {"status": VisitStatus.CANCELLED}
                        and visit.status == VisitStatus.CANCELLED
                    ):
                        return httpx.Response(200, json=visit.model_dump(mode="json"))
                    if visit.status != VisitStatus.PLANNED:
                        return self.error(409)
                    if not self.doctors[visit.doctor.id].is_active and body != {
                        "status": VisitStatus.CANCELLED
                    }:
                        return self.error(409)
                    changes = {k: v for k, v in body.items() if k != "doctor_id"}
                    if data.doctor_id is not None:
                        if (
                            data.doctor_id not in self.doctors
                            or not self.doctors[data.doctor_id].is_active
                        ):
                            return self.error(400)
                        changes["doctor"] = self.doctors[data.doctor_id]
                    changes["updated_at"] = self.clock.now().replace(second=0, microsecond=0)
                    visit = visit.model_copy(update=changes)
                    self.records[token][item_id] = visit
                    self.patch_count += 1
                    failure, self.next_failure = self.next_failure, None
                    if failure == "lost_response":
                        raise httpx.ReadTimeout("ответ потерян", request=request)
                    if failure == "server_error":
                        return self.error(500)
                    if failure == "invalid_response":
                        return httpx.Response(200, json={"unexpected": True})
                    return httpx.Response(200, json=visit.model_dump(mode="json"))
            except (ValidationError, ValueError, TypeError, AttributeError):
                return self.error(400)
            return self.error(404)

    def _limit(self, token: str) -> httpx.Response | None:
        if self.rate_limit is None:
            return None
        now = self.clock.now()
        hits = self._hits.setdefault(token, deque())
        while hits and hits[0] <= now - timedelta(seconds=self.rate_window_s):
            hits.popleft()
        if len(hits) >= self.rate_limit:
            self.limited += 1
            retry = max(1, math.ceil(self.rate_window_s - (now - hits[0]).total_seconds()))
            return httpx.Response(
                429,
                headers={"Retry-After": str(retry)},
                json={"code": "СЛИШКОМ_МНОГО_ЗАПРОСОВ", "message": "Отказ эмулятора"},
            )
        hits.append(now)
        return None

    def _list(self, resource: str, token: str, query: dict[str, str]) -> httpx.Response:
        limit, offset = int(query.get("limit", "50")), int(query.get("offset", "0"))
        if not 1 <= limit <= 50 or not 0 <= offset <= 9007199254740991:
            return self.error(400)
        if resource == "doctors":
            values = list(self.doctors.values())
            specialty = query.get("specialty", "").strip().casefold()
            values = [v for v in values if not specialty or v.specialty.casefold() == specialty]
            values.sort(key=lambda v: v.id)
        elif resource == "clients":
            values = list(self.clients[token].values())
            for key in ("last_name", "first_name"):
                if query.get(key, "").strip():
                    values = [v for v in values if getattr(v, key) == normalize_name(query[key])]
            if query.get("birth_date", "").strip():
                values = [v for v in values if v.birth_date == parse_date(query["birth_date"])]
            values.sort(key=lambda v: v.id)
        elif resource == "escalations":
            values = list(self.escalations[token].values())
            if "client_id" in query:
                if int(query["client_id"]) not in self.clients[token]:
                    return self.error(404)
                values = [v for v in values if v.client_id == int(query["client_id"])]
            # Как стенд: от новых к старым, при равном времени — по убыванию id
            values.sort(key=lambda v: (v.created_at, v.id), reverse=True)
        elif resource == "visits":
            values = list(self.records[token].values())
            for key, known in (("client_id", self.clients[token]), ("doctor_id", self.doctors)):
                if key in query:
                    item_id = int(query[key])
                    if item_id not in known:
                        return self.error(404)
                    values = [
                        v
                        for v in values
                        if (v.client_id if key == "client_id" else v.doctor.id) == item_id
                    ]
            if query.get("status", "").strip():
                status = VisitStatus(query["status"].strip())
                values = [v for v in values if v.status == status]
            for key in ("date_from", "date_to"):
                if query.get(key, "").strip():
                    boundary: date = parse_date(query[key])
                    values = [
                        v
                        for v in values
                        if (
                            v.scheduled_at.date() >= boundary
                            if key == "date_from"
                            else v.scheduled_at.date() <= boundary
                        )
                    ]
            values.sort(key=lambda v: (v.scheduled_at, v.id))
        else:
            return self.error(404)
        return httpx.Response(
            200, json=[v.model_dump(mode="json") for v in values[offset : offset + limit]]
        )
