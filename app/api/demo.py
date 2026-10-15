"""Браузерный прототип работает через тот же автономный разговорный backend."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
PAGE = Path(__file__).resolve().parents[1] / "demo_assets" / "demo.html"


@router.get("/demo", include_in_schema=False)
async def demo_page():
    return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})
