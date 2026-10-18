"""Настройки окружения: проверяем их до открытия внешних соединений."""

from enum import StrEnum
from pathlib import Path
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
    # Сколько один ход разговора ждёт окна МИС (шлюз ждёт ответа 15 с)
    # и сколько звонок ждёт МИС в сумме, прежде чем передать регистратору.
    mis_turn_wait_s: float = Field(default=8.0, ge=0, le=12)
    mis_wait_limit_s: int = Field(default=120, ge=10, le=600)
    # Время записей задаёт стенд; часовой пояс компьютера участника на него не влияет.
    clinic_utc_offset: Literal["+04:00"] = "+04:00"

    postgres_host: str = Field(default="localhost", min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(default="clinic", min_length=1)
    postgres_user: str = Field(default="clinic", min_length=1)
    postgres_password: SecretStr = SecretStr("clinic_dev")
    database_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    # Вход команды в публичное демо. Не заменяет идентификацию пациента и роли клиники.
    web_access_enabled: bool = False
    web_access_user: str = "clinic"
    web_access_password: SecretStr = SecretStr("")
    # Тексты диагностических событий доступны только команде, не более часа.
    debug_panel_enabled: bool = False

    # Текстовый HTTP-стенд включается явно и работает только с автономной МИС.
    backend_demo_enabled: bool = False
    # Внешний разговорный backend включается отдельно от учебной страницы.
    dialogue_enabled: bool = False
    dialogue_mode: Literal["commands", "natural"] = "commands"
    dialogue_storage: Literal["memory", "postgres"] = "memory"
    dialogue_hash_key: SecretStr = SecretStr("")
    schedule_path: Path | None = None
    schedule_index_path: Path | None = None
    schedule_access_path: Path | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.debug_panel_enabled and not self.web_access_enabled:
            raise ValueError("для DEBUG_PANEL_ENABLED нужен защищённый вход WEB_ACCESS_ENABLED")
        if self.debug_panel_enabled and not (self.backend_demo_enabled or self.dialogue_enabled):
            raise ValueError("для DEBUG_PANEL_ENABLED нужен включённый диалог")
        if self.web_access_enabled and (
            not self.web_access_user
            or len(self.web_access_user) > 64
            or any(c in self.web_access_user for c in ":\r\n\x00")
            or len(self.web_access_password.get_secret_value().encode()) < 24
        ):
            raise ValueError("для WEB_ACCESS_ENABLED нужны имя и WEB_ACCESS_PASSWORD от 24 байт")
        if self.schedule_path is not None and self.schedule_index_path is not None:
            raise ValueError("выберите один источник: SCHEDULE_PATH или SCHEDULE_INDEX_PATH")
        if self.schedule_access_path is not None and self.schedule_index_path is None:
            raise ValueError("SCHEDULE_ACCESS_PATH применяется только к SQLite-индексу")
        if self.backend_demo_enabled and self.app_mode != AppMode.OFFLINE_TEST:
            raise ValueError("командный HTTP-стенд доступен только в offline_test")
        if self.dialogue_enabled and self.app_mode != AppMode.HACKATHON:
            raise ValueError("DIALOGUE_ENABLED применяется только к режиму hackathon")
        if self.dialogue_enabled and (
            self.dialogue_mode != "natural"
            or self.dialogue_storage != "postgres"
            or self.schedule_index_path is None
        ):
            raise ValueError("для внешнего диалога нужны natural, postgres и SCHEDULE_INDEX_PATH")
        if (
            (self.backend_demo_enabled or self.dialogue_enabled)
            and self.dialogue_storage == "postgres"
            and len(self.dialogue_hash_key.get_secret_value().encode()) < 32
        ):
            raise ValueError("для долговечных квитанций нужен DIALOGUE_HASH_KEY от 32 байт")
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
