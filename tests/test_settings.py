"""Ошибки конфигурации должны обнаруживаться до соединения со стендом."""

import pytest
from pydantic import ValidationError

from app.settings import AppMode, Settings


def test_hackathon_requires_token():
    with pytest.raises(ValidationError, match="MIS_TOKEN"):
        Settings(_env_file=None, app_mode="hackathon", mis_token="  ")


def test_hackathon_accepts_token_without_exposing_it():
    settings = Settings(_env_file=None, app_mode="hackathon", mis_token="test-secret-token")
    assert settings.app_mode == AppMode.HACKATHON
    assert "test-secret-token" not in repr(settings)
    assert "test-secret-token" not in settings.model_dump_json()


def test_clinic_pilot_is_disabled():
    with pytest.raises(ValidationError, match="clinic_pilot"):
        Settings(_env_file=None, app_mode="clinic_pilot")


@pytest.mark.parametrize("value", [0, 9, 10])
def test_request_budget_preserves_reserve(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mis_rate_limit_per_min=value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/api/v1",
        "https://user:password@example.test/api/v1",
        "https://example.test/api/v1?token=secret",
        "https://example.test/api/v1#fragment",
    ],
)
def test_mis_base_url_rejects_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mis_base_url=url)


def test_clinic_timezone_cannot_follow_host_timezone():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, clinic_utc_offset="+03:00")


def test_environment_and_utf8_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "APP_MODE=offline_test\nPOSTGRES_PASSWORD=пароль_для_теста\n", encoding="utf-8"
    )
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    settings = Settings(_env_file=dotenv)
    assert settings.postgres_port == 5433
    assert settings.postgres_password.get_secret_value() == "пароль_для_теста"


def test_validation_error_does_not_print_secrets():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, postgres_port=0, postgres_password="secret-password")
    assert "secret-password" not in str(caught.value)


def test_settings_are_read_only():
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.app_mode = AppMode.HACKATHON
