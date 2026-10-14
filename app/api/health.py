"""Различаем живой процесс и готовность его зависимостей обслуживать запросы."""

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.bootstrap import Runtime
from app.ports import DatabaseUnavailable
from app.settings import AppMode

router = APIRouter(prefix="/health", tags=["Состояние сервиса"])


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    mode: AppMode
    checked_at: datetime
    database: Literal["ready", "unavailable"] | None = None


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDependency = Annotated[Runtime, Depends(get_runtime)]


@router.get("/live", response_model=HealthResponse, response_model_exclude_none=True)
async def live(runtime: RuntimeDependency) -> HealthResponse:
    """Процесс отвечает. Проверка не открывает соединение с МИС или PostgreSQL."""
    return HealthResponse(
        status="ok", mode=runtime.settings.app_mode, checked_at=runtime.clock.now()
    )


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "База пока недоступна"}},
)
async def ready(response: Response, runtime: RuntimeDependency) -> HealthResponse:
    try:
        await asyncio.wait_for(
            runtime.database.ping(), timeout=runtime.settings.database_probe_timeout_seconds
        )
    except (DatabaseUnavailable, TimeoutError):
        # Ошибка драйвера может содержать реквизиты соединения: возвращаем только безопасный статус.
        response.status_code = 503
        return HealthResponse(
            status="unavailable",
            mode=runtime.settings.app_mode,
            checked_at=runtime.clock.now(),
            database="unavailable",
        )
    return HealthResponse(
        status="ok",
        mode=runtime.settings.app_mode,
        checked_at=runtime.clock.now(),
        database="ready",
    )
