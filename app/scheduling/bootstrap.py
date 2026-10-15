"""Выбор источника расписания и привязка индекса к режиму и токену команды."""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from app.booking.schedule import JsonSchedule, unique_fields
from app.ports import Clock
from app.scheduling.index import SqliteSchedule
from app.scheduling.models import AccessRules, ScheduleImportError
from app.settings import AppMode, Settings


def token_scope(settings: Settings) -> str | None:
    if settings.app_mode == AppMode.OFFLINE_TEST:
        return None
    return hashlib.sha256(settings.mis_token.get_secret_value().encode()).hexdigest()


def read_document(path: Path, model):
    try:
        with path.open("rb") as stream:
            data = stream.read(4_000_001)
        if len(data) > 4_000_000:
            raise ScheduleImportError("DOCUMENT_TOO_LARGE")
        return model.model_validate(json.loads(data, object_pairs_hook=unique_fields))
    except (OSError, ValueError, ValidationError):
        raise ScheduleImportError("INVALID_DOCUMENT") from None


def load_index(path: Path, settings: Settings, clock: Clock) -> SqliteSchedule:
    facts = (
        read_document(settings.schedule_access_path, AccessRules).facts
        if settings.schedule_access_path is not None
        else ()
    )
    return SqliteSchedule(
        path,
        clock,
        mode=settings.app_mode.value,
        base_url=str(settings.mis_base_url),
        token_scope=token_scope(settings),
        facts=facts,
    )


def configured_schedule(settings: Settings, clock: Clock):
    if settings.schedule_index_path is not None:
        return load_index(settings.schedule_index_path, settings, clock)
    if settings.schedule_path is not None:
        return JsonSchedule.load(settings.schedule_path, clock)
    raise ScheduleImportError("SCHEDULE_REQUIRED")
