"""Единый ответ на недоступность МИС; сохраняем достигнутое состояние сценария."""

import math
from datetime import datetime, timedelta

from app.dialogue.models import DialogueState, Phase, Reply, Turn, reply
from app.mis.ports import BudgetCancelled, MisError

# Фазы, где пациент уже дал согласие на изменение записи.
CONFIRMING = {Phase.HELD, Phase.CANCEL_CONFIRM, Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}


def wait_phrases(state: DialogueState, seconds: int) -> tuple[str, ...]:
    rounded = max(5, math.ceil(seconds / 5) * 5)
    if state.phase == Phase.MOVE_FINISH_CONFIRM:
        # Новая запись уже создана; ждём окна, чтобы отменить прежнюю.
        return (
            "Система записи сейчас занята, прежняя запись пока не отменена.",
            f"Подождите около {rounded} секунд и подтвердите ещё раз.",
        )
    if state.phase in CONFIRMING:
        # Старое согласие после долгой паузы не используем: нужно новое «да».
        return (
            "Система записи сейчас занята, изменения не отправлены.",
            f"Подождите около {rounded} секунд и подтвердите ещё раз.",
        )
    if state.phase in {Phase.OFFERED, Phase.MOVE_OFFERED} and state.options:
        # Варианты уже названы: повторять подбор не нужно, достаточно выбора.
        return (
            "Система записи сейчас занята.",
            f"Подождите около {rounded} секунд и назовите выбранный вариант ещё раз.",
        )
    if state.phase == Phase.IDENTIFYING:
        return (
            "Система записи сейчас занята.",
            f"Подождите около {rounded} секунд и назовите ФИО и дату рождения ещё раз.",
        )
    # «Ещё раз» NLU понимает как просьбу повторить: подбор или справка запускаются заново.
    return (
        "Система записи сейчас занята.",
        f"Подождите около {rounded} секунд и скажите «ещё раз».",
    )


def mis_failure(
    state: DialogueState, turn: Turn, now: datetime, error: MisError
) -> tuple[DialogueState, Reply]:
    if state.purpose == "visits":
        state = state.model_copy(update={"visit_items": ()})
    if isinstance(error, BudgetCancelled):
        # Новая речь или отбой во время ожидания: этот ответ заменит следующая реплика.
        return state, reply(
            state, turn.turn_id, "Поступила новая реплика.", code="TURN_INTERRUPTED"
        )
    if error.retry_after is not None:
        state = state.model_copy(
            update={
                "retry_at": now + timedelta(seconds=error.retry_after),
                "mis_wait_since": state.mis_wait_since or now,
            }
        )
        return state, reply(
            state, turn.turn_id, *wait_phrases(state, error.retry_after), code="WAIT_MIS"
        )
    return state, reply(
        state, turn.turn_id, "МИС недоступна. Действие остановлено.", code="MIS_UNAVAILABLE"
    )
