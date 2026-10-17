"""Текстовый вход автономного стенда: команды или обычные фразы по настройке."""

from typing import Annotated

from asyncpg import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.dialogue.models import CallId, DialogueError, DialogueState, InputStart, Reply, Turn
from app.dialogue.service import DialogueService

router = APIRouter(prefix="/api/dialog", tags=["Автономный текстовый стенд"])


def get_service(request: Request) -> DialogueService:
    service = getattr(request.app.state, "dialogue", None)
    if service is None:
        raise HTTPException(404, detail="COMMAND_BACKEND_DISABLED")
    return service


Service = Annotated[DialogueService, Depends(get_service)]


@router.post("/{call_id}/input-started", response_model=Reply)
async def input_started(call_id: CallId, body: InputStart, service: Service) -> Reply:
    try:
        return await service.input_started(call_id, body.turn_id)
    except DialogueError as error:
        raise HTTPException(409, detail=str(error)) from None
    except (SQLAlchemyError, PostgresError, OSError):
        raise HTTPException(503, detail="DIALOGUE_STORAGE_UNAVAILABLE") from None


@router.post(
    "/{call_id}/turn",
    response_model=Reply,
    description=(
        "Вымышленная МИС: команды либо обычные фразы для записи С-01 и справки С-05. "
        "Режим задаётся на сервере; создание требует итогового подтверждения и проверки safety."
    ),
)
async def turn(call_id: CallId, body: Turn, service: Service) -> Reply:
    try:
        return await service.handle(call_id, body)
    except DialogueError as error:
        raise HTTPException(409, detail=str(error)) from None
    except (SQLAlchemyError, PostgresError, OSError):
        raise HTTPException(503, detail="DIALOGUE_STORAGE_UNAVAILABLE") from None


@router.get("/{call_id}", response_model=DialogueState, response_model_exclude={"identity_draft"})
async def inspect(call_id: CallId, service: Service) -> DialogueState:
    try:
        return await service.store.inspect(call_id)
    except (SQLAlchemyError, PostgresError, OSError):
        raise HTTPException(503, detail="DIALOGUE_STORAGE_UNAVAILABLE") from None


@router.post(
    "/{call_id}/end",
    response_model=Reply,
    description="Идемпотентное завершение разговора со снятием только местных броней.",
)
async def end(call_id: CallId, service: Service) -> Reply:
    try:
        return await service.end(call_id)
    except (SQLAlchemyError, PostgresError, OSError):
        raise HTTPException(503, detail="DIALOGUE_STORAGE_UNAVAILABLE") from None
