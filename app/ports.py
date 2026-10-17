"""Интерфейсы заменяемых ресурсов; HTTP-слой не зависит от драйвера базы."""

from datetime import datetime
from typing import Protocol


class DatabaseUnavailable(Exception):
    """База недоступна; наружу не передаются адрес, пароль и ошибка драйвера."""


class Database(Protocol):
    async def ping(self) -> None:
        """Проверить соединение или поднять DatabaseUnavailable."""
        ...

    async def aclose(self) -> None:
        """Освободить соединения при остановке приложения."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Вернуть момент с часовым поясом, без зависимости от локального времени ОС."""
        ...
