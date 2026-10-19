"""Настройки заявок (переменные ESCALATIONS_*)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EscalationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ESCALATIONS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Клиент для экстренных заявок без идентификации: заводится заранее командой
    # python -m app.escalations.cli technical-client, на критическом пути не создаётся
    technical_client_id: int | None = Field(default=None, ge=1)
