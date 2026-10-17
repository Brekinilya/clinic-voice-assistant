"""Изолированный стенд сценариев: журнал HTTP, квота, отказы и новые методы OpenAPI.

Не заменяет рабочий адаптер МИС и не открывает внешних соединений. Полные тела
запросов и заголовки авторизации в журнал измерений не попадают.
"""

import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import timedelta

import httpx

from app.mis.emulator import MisEmulator
from app.mis.models import format_datetime, normalize_name, parse_datetime


@dataclass
class RequestRecord:
    method: str
    resource: str
    status: int | None
    at: str
    offset: int | None = None
    limit: int | None = None
    fault: str | None = None
    source: str = "dialogue"
    query_keys: tuple[str, ...] = ()


def valid_text(value, maximum, *, nonempty=False):
    if not isinstance(value, str) or len(value) > maximum:
        return False
    if nonempty and not value.strip():
        return False
    return not any(
        ord(char) < 32
        and char not in "\t\n\r"
        or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )


class ScenarioEmulator(MisEmulator):
    """Квота внешнего сервера отделена от внутреннего бюджета приложения."""

    def __init__(self, clock, doctors, clients, *, limit=None, faults=()):
        super().__init__(clock, doctors, clients)
        self.limit = limit
        self.requests: list[RequestRecord] = []
        self.windows = {token: deque() for token in clients}
        self.escalations = {token: {} for token in clients}
        self.escalation_count = 0
        self.client_patch_count = 0
        self.faults = [dict(fault) for fault in faults]

    async def handle(self, request):
        resource = request.url.path.removeprefix("/api/v1/").strip("/")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        record = RequestRecord(request.method, resource, None, self.clock.now().isoformat())
        record.query_keys = tuple(sorted(request.url.params.keys()))
        for name in ("offset", "limit"):
            value = request.url.params.get(name)
            if value is not None and value.isdecimal():
                setattr(record, name, int(value))
        self.requests.append(record)
        try:
            if token in self.windows and self.limit is not None:
                window = self.windows[token]
                now = self.clock.now()
                while window and window[0] <= now - timedelta(seconds=60):
                    window.popleft()
                if len(window) >= self.limit:
                    delay = max(1, math.ceil(60 - (now - window[0]).total_seconds()))
                    response = httpx.Response(429, headers={"Retry-After": str(delay)})
                    record.status = response.status_code
                    return response
                window.append(now)
            for fault in self.faults:
                matches = (
                    fault.get("call") == "create_visit"
                    and resource == "visits"
                    and request.method == "POST"
                )
                if matches and fault.get("times", 1) > 0:
                    fault["times"] = fault.get("times", 1) - 1
                    self.clock.value += timedelta(seconds=fault.get("delay_s", 0))
                    record.fault = fault.get("kind", "timeout_before_write")
                    if record.fault == "lost_response":
                        self.next_failure = "lost_response"
                    else:
                        raise httpx.ReadTimeout("Синтетический отказ МИС", request=request)
                    break
            if token not in self.clients:
                response = self.error(401)
            elif resource == "escalations" or resource.startswith("escalations/"):
                async with self._lock:
                    response = self._escalation(request, resource, token)
            elif resource.startswith("clients/") and request.method == "PATCH":
                async with self._lock:
                    response = self._patch_client(request, resource, token)
            else:
                response = await super().handle(request)
            record.status = response.status_code
            return response
        except httpx.TransportError:
            record.fault = record.fault or "transport_error"
            raise

    def _escalation(self, request, resource, token):
        try:
            if request.method == "POST" and resource == "escalations":
                data = json.loads(request.content)
                client_id = data.get("client_id")
                description = data.get("description")
                if (
                    type(client_id) is not int
                    or client_id not in self.clients[token]
                    or not valid_text(description, 1000, nonempty=True)
                ):
                    return self.error(400)
                items = self.escalations[token]
                identifier = max(items, default=0) + 1
                item = {
                    "id": identifier,
                    "client_id": client_id,
                    "description": description,
                    "created_at": format_datetime(
                        self.clock.now().replace(second=0, microsecond=0)
                    ),
                }
                items[identifier] = item
                self.escalation_count += 1
                return httpx.Response(201, json=item)
            if request.method == "GET" and resource == "escalations":
                query = request.url.params
                limit, offset = int(query.get("limit", 50)), int(query.get("offset", 0))
                if not 1 <= limit <= 50 or not 0 <= offset <= 9007199254740991:
                    return self.error(400)
                client_id = int(query["client_id"]) if "client_id" in query else None
                if client_id is not None and client_id not in self.clients[token]:
                    return self.error(404)
                items = [
                    item
                    for item in self.escalations[token].values()
                    if client_id is None or item["client_id"] == client_id
                ]
                items.sort(
                    key=lambda item: (parse_datetime(item["created_at"]), item["id"]), reverse=True
                )
                return httpx.Response(200, json=items[offset : offset + limit])
            if request.method == "GET" and resource.count("/") == 1:
                identifier = int(resource.split("/")[1])
                item = self.escalations[token].get(identifier)
                return httpx.Response(200, json=item) if item else self.error(404)
        except (ValueError, TypeError, AttributeError):
            return self.error(400)
        return self.error(404)

    def _patch_client(self, request, resource, token):
        try:
            identifier = int(resource.split("/")[1])
            if identifier not in self.clients[token]:
                return self.error(404)
            data = json.loads(request.content)
            if not isinstance(data, dict):
                return self.error(400)
            if any(key in data for key in ("last_name", "first_name", "birth_date")):
                return self.error(400)
            changes = {}
            for key in ("middle_name", "comment"):
                if key not in data:
                    continue
                value = data[key]
                if value is None:
                    changes[key] = None
                    continue
                if not isinstance(value, str):
                    return self.error(400)
                if key == "middle_name":
                    # Ограничение ФИО применяется после нормализации пробелов/NFC.
                    value = normalize_name(value) if value.strip() else None
                else:
                    if not valid_text(value, len(value)):
                        return self.error(400)
                    if not value.strip():
                        value = None
                    elif len(value) > 1000:
                        return self.error(400)
                changes[key] = value
            item = self.clients[token][identifier].model_copy(update=changes)
            self.clients[token][identifier] = item
            self.client_patch_count += bool(changes)
            return httpx.Response(200, json=item.model_dump(mode="json"))
        except (ValueError, TypeError, AttributeError):
            return self.error(400)

    def request_summary(self):
        counts = {}
        for record in self.requests:
            key = record.method + " /" + record.resource.split("/")[0]
            counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(self.requests),
            "by_method_resource": counts,
            "requests": [asdict(record) for record in self.requests],
        }
