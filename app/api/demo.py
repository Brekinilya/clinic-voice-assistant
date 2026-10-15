"""Браузерный прототип работает через тот же автономный разговорный backend."""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
chat_router = APIRouter()
PAGE = Path(__file__).resolve().parents[1] / "demo_assets" / "demo.html"


@router.get("/demo", include_in_schema=False)
async def demo_page(request: Request):
    return await chat_page(request)


@chat_router.get("/chat", include_in_schema=False)
async def chat_page(request: Request):
    settings = request.app.state.runtime.settings
    config = {
        "demo": settings.backend_demo_enabled,
        "voice": hasattr(request.app.state, "voice"),
        "specialty": "хирургу" if settings.schedule_index_path else "терапевту",
        "workbook": settings.schedule_index_path is not None,
    }
    content = PAGE.read_text(encoding="utf-8").replace(
        "__CHAT_CONFIG__", json.dumps(config, ensure_ascii=False)
    )
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@chat_router.get("/", include_in_schema=False)
async def start_page():
    return RedirectResponse("/chat")
