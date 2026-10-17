"""Публичный вход закрывает API и WebSocket до исполнения команд диалога."""

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.api.access import TeamAccess
from app.main import create_app
from app.settings import Settings

PASSWORD = "fictional-demo-password-for-unit-tests"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"clinic:{PASSWORD}".encode()).decode()}


class Database:
    async def ping(self):
        pass

    async def aclose(self):
        pass


def app():
    return create_app(
        Settings(
            _env_file=None,
            web_access_enabled=True,
            web_access_password=PASSWORD,
            backend_demo_enabled=True,
            dialogue_mode="natural",
            dialogue_storage="memory",
        ),
        database_factory=lambda _: Database(),
    )


@pytest.mark.parametrize(
    "path",
    ["/", "/chat", "/demo", "/docs", "/openapi.json", "/api/dialog/private", "/web/voice.html"],
)
def test_anonymous_cannot_read_pages_or_dialogues(path):
    with TestClient(app()) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"
    assert PASSWORD not in response.text


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
def test_only_read_health_is_public(path):
    with TestClient(app()) as client:
        assert client.get(path).status_code == 200
        assert client.post(path).status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "Basic !!!", "Basic /w==", "Basic dXNlcg==", "Bearer test-token", "Basic " + "x" * 5000],
)
def test_invalid_credentials_fail_closed_without_echoing_input(header):
    with TestClient(app()) as client:
        response = client.get("/chat", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == {"detail": "ACCESS_REQUIRED"}


def test_password_in_url_or_cookie_cannot_replace_authorization_header():
    with TestClient(app()) as client:
        assert (
            client.get(
                "/chat?password=" + PASSWORD, headers={"Cookie": "password=" + PASSWORD}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/chat",
                headers=[
                    ("Authorization", AUTH["Authorization"]),
                    ("Authorization", AUTH["Authorization"]),
                ],
            ).status_code
            == 401
        )
        assert client.get("/chat", auth=("other", PASSWORD)).status_code == 401
        assert client.get("/chat", auth=("clinic", "wrong")).status_code == 401


def test_protected_turn_not_executed_then_authenticated_dialogue_works():
    with TestClient(app()) as client:
        path = "/api/dialog/access-test"
        body = {"turn_id": 1, "text": "Хочу записаться к терапевту"}
        assert client.post(path + "/turn", json=body).status_code == 401
        assert client.get(path, headers=AUTH).json()["phase"] == "ready"
        assert client.get("/chat", headers=AUTH).status_code == 200
        result = client.post(path + "/turn", headers=AUTH, json=body)
        assert result.status_code == 200
        assert result.json()["phase"] == "identifying"


@pytest.mark.parametrize(
    "origin",
    ["https://other.test", "null", "http://testserver.other", "https://testserver@other.test"],
)
def test_foreign_browser_origin_cannot_send_turn_with_cached_credentials(origin):
    with TestClient(app()) as client:
        response = client.post(
            "/api/dialog/csrf-test/turn",
            headers=AUTH | {"Origin": origin},
            json={"turn_id": 1, "text": "Хочу записаться к терапевту"},
        )
        assert response.status_code == 403
        assert client.get("/api/dialog/csrf-test", headers=AUTH).json()["phase"] == "ready"


def test_same_origin_browser_can_send_turn():
    with TestClient(app()) as client:
        response = client.post(
            "/api/dialog/same-origin/turn",
            headers=AUTH | {"Origin": "https://testserver"},
            json={"turn_id": 1, "text": "Хочу записаться к терапевту"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "identifying"


@pytest.mark.parametrize("headers", [{}, AUTH | {"Origin": "https://other.test"}])
def test_websocket_cannot_bypass_gate(headers):
    async def socket(ws):
        pytest.fail("неавторизованный WebSocket дошёл до приложения")

    application = TeamAccess(
        Starlette(routes=[WebSocketRoute("/socket", socket)]), "clinic", PASSWORD
    )
    with TestClient(application) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/socket", headers=headers):
                pass
    assert caught.value.code == 1008


def test_authorized_same_origin_websocket_and_lifespan_pass_through():
    async def socket(ws):
        await ws.accept()
        await ws.send_text("ready")
        await ws.close()

    application = TeamAccess(
        Starlette(routes=[WebSocketRoute("/socket", socket)]), "clinic", PASSWORD
    )
    with TestClient(application) as client:
        with client.websocket_connect(
            "/socket", headers=AUTH | {"Origin": "https://testserver"}
        ) as ws:
            assert ws.receive_text() == "ready"


def test_incomplete_access_configuration_rejected_and_secrets_masked():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_access_enabled=True, web_access_password="too-short")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            web_access_enabled=True,
            web_access_user="bad:name",
            web_access_password=PASSWORD,
        )
    settings = Settings(_env_file=None, web_access_enabled=True, web_access_password=PASSWORD)
    assert PASSWORD not in repr(settings)
    assert PASSWORD not in settings.model_dump_json()
