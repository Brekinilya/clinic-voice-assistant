"""Настройки напоминаний (переменные REMINDERS_*)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class ReminderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REMINDERS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # До явного включения исполнитель не создаётся и автоматических вызовов нет.
    enabled: bool = False

    # Через шлюз хакатона можно звонить только своему софтфону
    allowed_endpoints: tuple[str, ...] = ("PJSIP/phone-021@gateway",)
