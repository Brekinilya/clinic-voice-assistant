"""Один тип ожидаемого ответа для NLU, текстового канала и голосового шлюза."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.contracts import ExpectedAnswer

if TYPE_CHECKING:
    from app.dialogue.models import DialogueState


def expected_answer(state: DialogueState) -> ExpectedAnswer:
    if state.medical_boundary_pending:
        return "yes_no"
    if state.phase == "identifying":
        # Наш шаг идентификации сейчас просит ФИО вместе с датой рождения.
        return "full_name"
    if state.phase in {"offered", "cancel_choosing", "move_choosing", "move_offered"}:
        return "choice"
    if state.phase in {
        "held",
        "verifying_owner",
        "visits_shown",
        "cancel_confirm",
        "move_source",
        "move_empty",
        "move_confirm",
        "move_finish_confirm",
    }:
        return "yes_no"
    return "free"
