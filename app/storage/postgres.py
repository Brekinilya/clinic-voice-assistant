"""PostgreSQL через SQLAlchemy: один небольшой пул на экземпляр приложения."""

from asyncpg import PostgresError
from sqlalchemy import URL, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.ports import DatabaseUnavailable
from app.settings import Settings


def connection_url(settings: Settings) -> URL:
    # URL.create экранирует специальные символы в пароле; строку вручную не склеиваем.
    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


class PostgresDatabase:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> "PostgresDatabase":
        engine = create_async_engine(
            connection_url(settings),
            pool_size=3,
            max_overflow=0,
            pool_timeout=settings.database_probe_timeout_seconds,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"timeout": settings.database_probe_timeout_seconds},
        )
        return cls(engine)

    async def ping(self) -> None:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, PostgresError, OSError) as error:
            raise DatabaseUnavailable("база данных недоступна") from error

    async def aclose(self) -> None:
        await self._engine.dispose()
