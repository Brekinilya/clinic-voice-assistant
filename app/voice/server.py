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
from app.voice.failures import failure
from app.voice.fallback import load_fallback
from app.voice.settings import VoiceSettings
from app.voice.telephony import TelephonySettings
from app.voice.work import SpeechRuntime

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
            runtime = SpeechRuntime(voice.inference_queue_capacity, voice.dialogue_job_capacity)
            app.state.voice_runtime = runtime
            try:
                # Если резервные файлы повреждены, readiness не должен обещать рабочий голос.
                load_fallback()
                load_fallback(True)
                async with phone_line(app, telephony):
                    yield
            finally:
                runtime.close()
                del app.state.voice_runtime
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
        return await app.state.voice_runtime.tts.run(
            synthesizer.synthesize, speech, timeout=app.state.voice_settings.tts_timeout_s
        )

    debug = getattr(app.state.dialogue, "debug", None)

    async def transfer_started(event) -> None:
        handoffs = getattr(app.state.dialogue, "handoffs", None)
        if handoffs is not None:
            dialogue_state = await app.state.dialogue.store.inspect(event.call_id)
            await handoffs.transfer_started(event, dialogue_state)

    async def transfer_result(result) -> None:
        handoffs = getattr(app.state.dialogue, "handoffs", None)
        if handoffs is not None:
            await handoffs.transfer_result(result)
        # До T08 исход перевода виден в панели; заявку по нему создаёт диалог, не шлюз
        if debug is not None:
            debug.emit(
                result.call_id,
                None,
                "voice.transfer_result",
                source="SIP",
                **{k: v for k, v in vars(result).items() if k != "call_id"},
            )

    ari = Ari(settings)
    line = Telephony(
        ari,
        settings,
        sessions=lambda call_id, channel: open_session(app.state, call_id, channel),
        say=say,
        resample=resample,
        on_transfer=transfer_result,
        on_transfer_start=transfer_started,
        outbound=lambda call_id, channel, context, machine, done: open_session(
            app.state,
            call_id,
            channel,
            outbound=context,
            machine=machine,
            on_outcome=done,
        ),
    )
    from app.reminders.settings import ReminderSettings

    reminders = ReminderSettings()
    worker = None
    if reminders.enabled:
        from uuid import uuid4

        from app.dialogue.reminder import ReminderConversation
        from app.reminders.calendar import ClinicCalendar
        from app.reminders.dialogue_adapter import ReminderActionAdapter
        from app.reminders.executor import ReminderExecutor
        from app.reminders.marks import ReminderMarks
        from app.storage.dialogue import PostgresDialogueStore
        from app.storage.reminders import PostgresReminderStore

        service = app.state.dialogue
        if not isinstance(service.store, PostgresDialogueStore) or service.conversation is None:
            await ari.aclose()
            raise RuntimeError("для обзвона нужны PostgreSQL и настоящий диалог")
        clock = service.booking.clock
        store = PostgresReminderStore(service.store.engine)
        owner = "voice-" + uuid4().hex
        marks = ReminderMarks(
            service.booking.mis, store, clock, owner=owner, require_dialogue_guard=True
        )
        executor = ReminderExecutor(
            store,
            service.booking.mis,
            ClinicCalendar.load(),
            clock,
            line,
            owner=owner,
            allowed_endpoints=reminders.allowed_endpoints,
            marks=marks,
            rooms=service.conversation.rooms,
        )
        service.reminders = ReminderConversation(
            service.conversation, ReminderActionAdapter(executor, marks, store, clock)
        )
        line.reminders = executor
        app.state.reminder_executor = executor

        async def run_reminders():
            while True:
                try:
                    # Входящий разговор не прерываем ради фонового обзвона.
                    if ari.connected.is_set() and (executor.active is not None or not line.calls):
                        await executor.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Граница фонового обзвона: следующий тик через 15 с, попытки ведёт исполнитель
                    failure("voice.reminders_tick_failed", error)
                await asyncio.sleep(15)

        worker = asyncio.create_task(run_reminders())
    listener = asyncio.create_task(line.run())
    try:
        yield
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        listener.cancel()
        await line.close()
        await ari.aclose()
