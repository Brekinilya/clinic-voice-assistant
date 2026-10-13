"""Фабрика приложения: каждый запуск и каждый тест получают собственные ресурсы."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.bootstrap import build_runtime
from app.ports import Clock, Database
from app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    database_factory: Callable[[Settings], Database] | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    selected_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Пул создаётся при запуске, а не при импорте. Подмена для теста передаётся явно.
        runtime = build_runtime(selected_settings, database_factory, clock)
        application.state.runtime = runtime
        try:
            yield
        finally:
            try:
                await runtime.database.aclose()
            finally:
                del application.state.runtime

    application = FastAPI(
        title="Голосовой ассистент регистратуры",
        version="0.1.0",
        description="Каркас сервиса. Диалог и операции МИС подключаются отдельными компонентами.",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    return application
