"""Автономный HTTP-стенд только для методов нашего адаптера; реальные данные запрещены."""

import asyncio
import json
from datetime import date
from urllib.parse import parse_qs

import httpx
from pydantic import ValidationError

from app.mis.models import (
    Client,
    Doctor,
    Visit,
    VisitChange,
    VisitCreate,
    VisitStatus,
    normalize_name,
    parse_date,
)
from app.ports import Clock


class MisEmulator:
    def __init__(
        self, clock: Clock, doctors: list[Doctor], clients: dict[str, list[Client]]
    ) -> None:
        self.clock = clock
        self.doctors = {item.id: item for item in doctors}
        self.clients = {
            token: {item.id: item for item in items} for token, items in clients.items()
        }
        self.records: dict[str, dict[int, Visit]] = {token: {} for token in clients}
        self.write_count = 0
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
            try:
                if len(path) == 1 and request.method == "GET":
                    return self._list(path[0], token, query)
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
                        else {}
                    )
                    if item_id not in items:
                        return self.error(404)
                    return httpx.Response(200, json=items[item_id].model_dump(mode="json"))
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
                    return httpx.Response(200, json=visit.model_dump(mode="json"))
            except (ValidationError, ValueError, TypeError, AttributeError):
                return self.error(400)
            return self.error(404)

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
