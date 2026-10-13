"""Фабрика приложения: каждый запуск и каждый тест получают собственные ресурсы."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.dialogue import router as dialogue_router
from app.api.health import router as health_router
from app.bootstrap import build_runtime
from app.dialogue.bootstrap import open_command_backend
from app.dialogue.service import DialogueService
from app.ports import Clock, Database
from app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    database_factory: Callable[[Settings], Database] | None = None,
    clock: Clock | None = None,
    dialogue_factory: Callable[[Settings, Clock], AbstractAsyncContextManager[DialogueService]]
    | None = None,
) -> FastAPI:
    selected_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Пул создаётся при запуске, а не при импорте. Подмена для теста передаётся явно.
        runtime = build_runtime(selected_settings, database_factory, clock)
        application.state.runtime = runtime
        try:
            async with AsyncExitStack() as resources:
                if selected_settings.backend_demo_enabled:
                    factory = (
                        dialogue_factory if dialogue_factory is not None else open_command_backend
                    )
                    application.state.dialogue = await resources.enter_async_context(
                        factory(selected_settings, runtime.clock)
                    )
                yield
        finally:
            try:
                await runtime.database.aclose()
            finally:
                del application.state.runtime
                if hasattr(application.state, "dialogue"):
                    del application.state.dialogue

    application = FastAPI(
        title="Голосовой ассистент регистратуры",
        version="0.1.0",
        description="Каркас сервиса. Диалог и операции МИС подключаются отдельными компонентами.",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    if selected_settings.backend_demo_enabled:
        application.include_router(dialogue_router)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_, error: RequestValidationError):
        # FastAPI по умолчанию включает исходный input: исключаем его для ФИО и даты рождения.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()
                ]
            },
        )

    return application
