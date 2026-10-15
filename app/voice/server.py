"""Голосовой стенд: приложение из app.main, голосовой шлюз, страница звонка и телефония.

    uv sync --locked --extra voice
    BACKEND_DEMO_ENABLED=true uv run uvicorn app.voice.server:create_voice_app --factory

Страница звонка — http://127.0.0.1:8000/web/voice.html. Модели — в models/, см. bench/speech/download.py.
С TELEPHONY_ENABLED=true стенд принимает звонки Asterisk через ARI (deploy/asterisk/README.md).
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.clock import ClinicClock
from app.main import create_app
from app.resource_paths import config_directory
from app.settings import Settings
from app.voice.api import open_session, router
from app.voice.settings import VoiceSettings
from app.voice.telephony import TelephonySettings

WEB = Path(__file__).resolve().parents[2] / "web"
if not WEB.is_dir():
    WEB = config_directory() / "web"


# Реплики без подстановок, которые звучат чаще всего: синтезируем при запуске, дальше — из кэша
FREQUENT = (
    "greeting.inbound",
    "ask.how_can_help",
    "wait.schedule",
    "reask.first",
    "silence.first",
    "silence.final",
    "transfer.mis_down",
    "closing.goodbye",
    "emergency.no_answer",
    "transfer.no_answer",
)


def load_engines(settings: VoiceSettings):
    from app.voice.engines import Engines, GigaAmRecognizer, SileroSynthesizer, SileroVad
    from app.voice.render import default_renderer, load_pronunciation

    engines = Engines(
        vad=SileroVad(settings.path(settings.vad_model)),
        recognizer=GigaAmRecognizer(
            settings.path(settings.asr_model),
            settings.path(settings.asr_config),
            settings.threads,
        ),
        synthesizer=SileroSynthesizer(
            settings.path(settings.tts_model),
            settings.tts_speaker,
            settings.tts_rate,
            load_pronunciation().intensity,
            settings.threads,
        ),
    )
    # Первый прогон моделей медленный: делаем его до первого звонка
    engines.recognizer.transcribe(bytes(32000))
    renderer = default_renderer()
    today = ClinicClock().now().date()
    for template in FREQUENT:
        text = renderer.render(template, today).text
        engines.synthesizer.synthesize(renderer.speak(text, today))
    return engines


def create_voice_app(
    settings: Settings | None = None,
    voice: VoiceSettings | None = None,
    engines=None,
    telephony: TelephonySettings | None = None,
    **options,
) -> FastAPI:
    """options уходят в create_app: подмена базы, часов и диалога в тестах."""
    application = create_app(settings, **options)
    voice = voice or VoiceSettings()
    telephony = telephony or TelephonySettings()
    inner = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with inner(app):
            app.state.voice_settings = voice
            app.state.voice = engines or await asyncio.to_thread(load_engines, voice)
            async with phone_line(app, telephony):
                try:
                    yield
                finally:
                    del app.state.voice

    application.router.lifespan_context = lifespan
    application.include_router(router)
    application.mount("/web", StaticFiles(directory=WEB), name="web")
    return application


@asynccontextmanager
async def phone_line(app: FastAPI, settings: TelephonySettings):
    """Звонки Asterisk на время работы стенда; без TELEPHONY_ENABLED ничего не делает."""
    if not settings.enabled:
        yield
        return
    if getattr(app.state, "dialogue", None) is None:
        raise RuntimeError("для телефонии нужен включённый разговорный backend")
    from app.voice.engines import resample
    from app.voice.render import default_renderer
    from app.voice.telephony import Ari, Telephony

    renderer = default_renderer()
    synthesizer = app.state.voice.synthesizer

    async def say(template: str) -> tuple[bytes, int]:
        today = ClinicClock().now().date()
        speech = renderer.speak(renderer.render(template, today).text, today)
        return await asyncio.to_thread(synthesizer.synthesize, speech)

    ari = Ari(settings)
    line = Telephony(
        ari,
        settings,
        sessions=lambda call_id, channel: open_session(app.state, call_id, channel),
        say=say,
        resample=resample,
    )
    listener = asyncio.create_task(line.run())
    try:
        yield
    finally:
        listener.cancel()
        await line.close()
        await ari.aclose()
