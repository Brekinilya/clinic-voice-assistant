"""URL соединения и отдельная проверка настоящего PostgreSQL для CI/локального сервера."""

import os

import pytest
from asyncpg import InvalidPasswordError
from sqlalchemy import make_url

from app.ports import DatabaseUnavailable
from app.settings import Settings
from app.storage.postgres import PostgresDatabase, connection_url


def test_password_special_characters_survive_url_encoding():
    settings = Settings(_env_file=None, postgres_password="p@ss:/?# пароль")
    url = connection_url(settings)
    assert url.password == "p@ss:/?# пароль"
    assert "p@ss" not in str(url)
    assert url.drivername == "postgresql+asyncpg"


async def test_native_driver_connection_error_is_normalized():
    class RejectedConnection:
        async def __aenter__(self):
            raise InvalidPasswordError("private-connection-details")

        async def __aexit__(self, *args):
            return False

    class RejectedEngine:
        def connect(self):
            return RejectedConnection()

    with pytest.raises(DatabaseUnavailable) as caught:
        await PostgresDatabase(RejectedEngine()).ping()
    assert "private-connection-details" not in str(caught.value)


@pytest.mark.postgres
async def test_real_postgres_probe():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("настоящий PostgreSQL не задан: нужен TEST_DATABASE_URL")
    parsed = make_url(url)
    settings = Settings(
        _env_file=None,
        postgres_host=parsed.host,
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username,
        postgres_password=parsed.password,
        postgres_db=parsed.database,
    )
    database = PostgresDatabase.from_settings(settings)
    try:
        await database.ping()
    finally:
        await database.aclose()
