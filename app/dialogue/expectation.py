"""Один тип ожидаемого ответа для NLU, текстового канала и голосового шлюза."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.contracts import ExpectedAnswer

if TYPE_CHECKING:
    from app.dialogue.models import DialogueState


class ActiveQuestion(BaseModel):
    """Смысл вопроса из состояния. Метаданные не заменяют разрешение домена."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    expect: ExpectedAnswer
    proposal_id: str | None = None
    proposal_revision: int | None = None
    option_ids: tuple[str, ...] = ()


def active_question(state: DialogueState) -> ActiveQuestion | None:
    """Одна проекция для NLU, ответа и диагностики, без чтения МИС и ПДн."""
    home_visit_questions = {
        "confirm": ("home_visit_continue", "yes_no"),
        "identify": ("patient_identity", "full_name"),
        "address": ("home_visit_address", "free"),
        "reason": ("home_visit_reason", "free"),
        "can_come": ("home_visit_can_come", "yes_no"),
        "contact": ("home_visit_contact", "yes_no"),
    }
    if state.home_visit_stage in home_visit_questions:
        key, expect = home_visit_questions[state.home_visit_stage]
        return ActiveQuestion(key=key, expect=expect)
    if state.information_clarifying:
        return ActiveQuestion(key="information_details", expect="free")
    if state.information_topic == "referral_missing":
        return ActiveQuestion(key="referral_missing", expect="yes_no")
    if state.information_topic is not None and state.phase in {
        "ready",
        "booked",
        "cancelled",
        "rescheduled",
    }:
        return ActiveQuestion(key="more_help", expect="yes_no")
    if state.medical_boundary_pending:
        return ActiveQuestion(key="medical_booking", expect="yes_no")
    if state.phase == "remind_details" and state.reminder and state.reminder.declined:
        return ActiveQuestion(key="reminder_cancel_or_move", expect="free")
    if state.phase == "identifying":
        return ActiveQuestion(
            key="birth_date" if state.identity_draft else "patient_identity",
            expect="birth_date" if state.identity_draft else "full_name",
        )
    if state.phase in {"offered", "move_offered"}:
        return ActiveQuestion(
            key="slot_choice",
            expect="choice",
            option_ids=tuple(
                f"slot-{slot.doctor_id}-{slot.scheduled_at:%Y%m%d%H%M}" for slot in state.options
            ),
        )
    if state.phase in {"cancel_choosing", "move_choosing"}:
        return ActiveQuestion(
            key="visit_choice",
            expect="choice",
            option_ids=tuple(f"visit-{item.visit_id}" for item in state.visit_items),
        )
    confirmations = {
        "held": ("booking_confirmation", state.proposal),
        "cancel_confirm": ("cancellation_confirmation", state.cancel_proposal),
        "move_confirm": ("move_confirmation", state.move_proposal),
        "move_finish_confirm": ("move_finish_confirmation", state.move_proposal),
    }
    if state.phase in confirmations:
        key, proposal = confirmations[state.phase]
        return ActiveQuestion(
            key=key,
            expect="yes_no",
            proposal_id=proposal.action_id if proposal else None,
            proposal_revision=proposal.revision if proposal else None,
        )
    questions = {
        "ready": ("goal", "free"),
        "identified": ("preferences" if state.specialty else "specialty", "free"),
        "booked": ("more_help", "yes_no"),
        "verifying_owner": ("visit_owner", "yes_no"),
        "visits_shown": ("visit_next_page" if state.visit_has_more else "more_help", "yes_no"),
        "cancel_offer": ("cancel_or_move", "free"),
        "move_source": ("move_source", "yes_no"),
        "move_empty": ("new_booking", "yes_no"),
        "remind_verify": ("reminder_addressee", "yes_no"),
        "remind_details": ("reminder_attendance", "yes_no"),
    }
    if state.phase in questions:
        key, expect = questions[state.phase]
        return ActiveQuestion(key=key, expect=expect)
    return None


def expected_answer(state: DialogueState) -> ExpectedAnswer:
    question = active_question(state)
    return question.expect if question else "free"
