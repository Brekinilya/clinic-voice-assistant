"""Закрытая панель команды. Наблюдение не даёт права менять состояние звонка."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from app.dialogue.models import CallId

router = APIRouter()
ASSETS = Path(__file__).resolve().parents[1] / "demo_assets" / "debug"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; object-src 'none'",
}


def recorder(request):
    value = getattr(request.app.state, "diagnostics", None)
    if value is None:
        raise HTTPException(404, "Диагностика не включена")
    return value


@router.get("/debug", include_in_schema=False)
@router.get("/debug/{asset}", include_in_schema=False)
async def page(request: Request, asset: str = "index.html"):
    recorder(request)
    if asset not in {"index.html", "panel.js", "panel.css"}:
        raise HTTPException(404)
    return FileResponse(ASSETS / asset, headers=HEADERS)


async def read_events(value, **kwargs):
    import asyncio

    try:
        async with asyncio.timeout(2):
            return await value.store.read(**kwargs)
    except Exception:
        raise HTTPException(503, "Журнал временно недоступен; звонки продолжают работать") from None


@router.get("/api/debug/calls")
async def calls(request: Request):
    value = recorder(request)
    import asyncio

    try:
        async with asyncio.timeout(2):
            items = await value.store.calls()
    except Exception:
        raise HTTPException(503, "Журнал временно недоступен") from None
    return JSONResponse(
        jsonable_encoder(
            {
                "calls": items,
                "recorder": value.status(),
            }
        ),
        headers=HEADERS,
    )


@router.get("/api/debug/waitlist")
async def waitlist(request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    recorder(request)
    service = getattr(request.app.state, "dialogue", None)
    store = getattr(getattr(service, "conversation", None), "waitlist_store", None)
    if store is None:
        raise HTTPException(404, "Лист ожидания не включён")
    import asyncio

    try:
        async with asyncio.timeout(2):
            requests = await store.recent(limit)
    except Exception:
        raise HTTPException(503, "Лист ожидания временно недоступен") from None
    return JSONResponse(
        jsonable_encoder({"requests": requests}),
        headers=HEADERS,
    )


@router.get("/api/debug/calls/{call_id}/events")
async def events(
    request: Request,
    call_id: CallId,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 300,
):
    value = recorder(request)
    rows = await read_events(value, call_id=call_id, after=after, limit=limit)
    return JSONResponse(
        jsonable_encoder(
            {
                "events": rows,
                "cursor": rows[-1]["id"] if rows else after,
                "has_more": len(rows) == limit,
                "recorder": value.status(),
            }
        ),
        headers=HEADERS,
    )
