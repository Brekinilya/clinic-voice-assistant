"""Общий вход команды: HTTP, документация и WebSocket защищаются одинаково."""

import base64
import binascii
import hashlib
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_HEALTH = frozenset({"/health/live", "/health/ready"})


class TeamAccess:
    def __init__(self, app: ASGIApp, username: str, password: str):
        self.app = app
        # Хеши фиксированной длины сравниваем без раннего выхода по имени пользователя.
        self.username = hashlib.sha256(username.encode()).digest()
        self.password = hashlib.sha256(password.encode()).digest()

    def authenticated(self, headers: Headers) -> bool:
        values = headers.getlist("authorization")
        if len(values) != 1 or len(values[0]) > 4096:
            return False
        scheme, separator, encoded = values[0].partition(" ")
        if not separator or scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        user_ok = secrets.compare_digest(hashlib.sha256(username.encode()).digest(), self.username)
        password_ok = secrets.compare_digest(
            hashlib.sha256(password.encode()).digest(), self.password
        )
        return user_ok & password_ok

    @staticmethod
    def same_origin(headers: Headers) -> bool:
        origins = headers.getlist("origin")
        if not origins:
            # Консольные клиенты без Origin всё равно предъявляют пароль.
            return True
        if len(origins) != 1:
            return False
        try:
            origin = urlsplit(origins[0])
            return (
                origin.scheme in {"http", "https"}
                and not origin.username
                and not origin.password
                and origin.netloc.casefold() == headers.get("host", "").casefold()
                and not origin.path
                and not origin.query
                and not origin.fragment
            )
        except ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if kind == "http" and scope["path"] in PUBLIC_HEALTH and scope["method"] in {"GET", "HEAD"}:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        authorized = self.authenticated(headers)
        origin_ok = self.same_origin(headers)
        if not authorized or not origin_ok:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse(
                {"detail": "ACCESS_REQUIRED" if not authorized else "ORIGIN_REJECTED"},
                status_code=401 if not authorized else 403,
                headers={
                    "Cache-Control": "no-store",
                    **(
                        {"WWW-Authenticate": 'Basic realm="clinic-demo", charset="UTF-8"'}
                        if not authorized
                        else {}
                    ),
                },
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
