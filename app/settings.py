"""Настройки окружения: проверяем их до открытия внешних соединений."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(StrEnum):
    OFFLINE_TEST = "offline_test"
    HACKATHON = "hackathon"
    CLINIC_PILOT = "clinic_pilot"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_mode: AppMode = AppMode.OFFLINE_TEST
    mis_base_url: HttpUrl = HttpUrl("https://crm-med-mock.dbi-testing.ru/api/v1")
    mis_token: SecretStr = SecretStr("")
    mis_rate_limit_per_min: int = Field(default=8, ge=1, le=8)
    # Время записей задаёт стенд; часовой пояс компьютера участника на него не влияет.
    clinic_utc_offset: Literal["+04:00"] = "+04:00"

    postgres_host: str = Field(default="localhost", min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(default="clinic", min_length=1)
    postgres_user: str = Field(default="clinic", min_length=1)
    postgres_password: SecretStr = SecretStr("clinic_dev")
    database_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.app_mode == AppMode.CLINIC_PILOT:
            raise ValueError("clinic_pilot ещё не разрешён: контур реальной клиники не подготовлен")
        if self.app_mode == AppMode.HACKATHON and not self.mis_token.get_secret_value().strip():
            raise ValueError("для режима hackathon нужен MIS_TOKEN")
        if self.mis_base_url.scheme != "https":
            raise ValueError("MIS_BASE_URL должен использовать HTTPS")
        if self.mis_base_url.username or self.mis_base_url.password:
            raise ValueError("учётные данные передаются через MIS_TOKEN, а не через MIS_BASE_URL")
        if self.mis_base_url.query or self.mis_base_url.fragment:
            raise ValueError("MIS_BASE_URL не должен содержать параметры запроса или фрагмент")
        if not self.postgres_password.get_secret_value():
            raise ValueError("POSTGRES_PASSWORD не должен быть пустым")
        return self
