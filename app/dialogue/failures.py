"""Единый ответ на недоступность МИС; сохраняем достигнутое состояние сценария."""

from datetime import datetime, timedelta

from app.dialogue.models import DialogueState, Reply, Turn, reply
from app.mis.ports import MisError


def mis_failure(
    state: DialogueState, turn: Turn, now: datetime, error: MisError
) -> tuple[DialogueState, Reply]:
    if state.purpose == "visits":
        state = state.model_copy(update={"visit_items": ()})
    if error.retry_after is not None:
        state = state.model_copy(update={"retry_at": now + timedelta(seconds=error.retry_after)})
        return state, reply(
            state,
            turn.turn_id,
            "Лимит запросов исчерпан. Повторите просьбу после указанного времени.",
            code="WAIT_MIS",
        )
    return state, reply(
        state, turn.turn_id, "МИС недоступна. Действие остановлено.", code="MIS_UNAVAILABLE"
    )
