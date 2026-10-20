"""Общие переходы памяти и PostgreSQL: только сервер меняет разрешение записи."""

from app.dialogue.models import (
    DialogueError,
    DialogueState,
    Phase,
    TransferRequest,
    WriteAuthorization,
    reply,
)
from app.dialogue.outbound import MarkOperation, ReminderResult, mark_action_id
from app.voice.phrases import load_phrasebook


def observe_input(state: DialogueState, turn_id: int, digest: str) -> DialogueState:
    if state.phase in {Phase.ENDED, Phase.HANDOFF} or turn_id < state.observed_turn_id:
        return state
    if turn_id == state.observed_turn_id and digest == state.observed_digest:
        return state
    return state.model_copy(
        update={
            "input_epoch": state.input_epoch + 1,
            "observed_turn_id": turn_id,
            "observed_digest": digest,
            "authorization": None,
        }
    )


def start_input(state: DialogueState, turn_id: int) -> DialogueState:
    if type(turn_id) is not int or turn_id < 1:
        raise DialogueError("INVALID_TURN_ID")
    if state.phase in {Phase.ENDED, Phase.HANDOFF} or turn_id <= max(
        state.observed_turn_id, state.last_turn_id
    ):
        return state
    return state.model_copy(
        update={
            "input_epoch": state.input_epoch + 1,
            "observed_turn_id": turn_id,
            "started_turn_id": turn_id,
            "observed_digest": None,
            "authorization": None,
            # Отменённые до расшифровки фразы не имеют квитанции. Их пропуск допустим
            # только после явного события начала речи, обычный HTTP остаётся строгим.
            "last_turn_id": max(state.last_turn_id, turn_id - 1)
            if state.pending_turn_id is None
            else state.last_turn_id,
        }
    )


def authorize_write(
    state: DialogueState,
    version: int,
    epoch: int,
    turn_id: int,
    *,
    reminder_operation: MarkOperation | None = None,
) -> DialogueState | None:
    if reminder_operation is not None:
        session = state.reminder
        if (
            session is None
            or not session.verified
            or state.phase in {Phase.ENDED, Phase.HANDOFF, Phase.INTERRUPTED}
            or state.client_id != session.binding.client_id
            or state.version != version
            or state.input_epoch != epoch
            or state.observed_turn_id != turn_id
            or state.pending_turn_id != turn_id
            or reminder_operation == "confirm"
            and (state.phase != Phase.REMIND_DETAILS or session.declined)
        ):
            return None
        return state.model_copy(
            update={
                "authorization": WriteAuthorization(
                    turn_id=turn_id,
                    version=version,
                    input_epoch=epoch,
                    action_id=mark_action_id(session.binding, reminder_operation),
                    revision=session.binding.fence,
                )
            }
        )
    proposal = state.cancel_proposal if state.phase == Phase.CANCEL_CONFIRM else state.proposal
    if state.phase in {Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}:
        proposal = state.move_proposal
    if (
        state.phase
        not in {Phase.HELD, Phase.CANCEL_CONFIRM, Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}
        or state.version != version
        or state.input_epoch != epoch
        or state.observed_turn_id != turn_id
        or state.pending_turn_id != turn_id
        or proposal is None
    ):
        return None
    return state.model_copy(
        update={
            "authorization": WriteAuthorization(
                turn_id=turn_id,
                version=version,
                input_epoch=epoch,
                action_id=proposal.action_id,
                revision=proposal.revision,
            )
        }
    )


def handoff_reply(state: DialogueState, turn_id: int):
    route = state.transfer.route if state.transfer else "R-08"
    if route == "R-06" and state.transfer and state.transfer.reason == "mis_wait":
        phrases = (
            "Система записи не освободилась. Автоматическая запись остановлена.",
            "Запрос регистратору подготовлен.",
        )
    elif route == "R-06" and state.transfer and state.transfer.reason:
        phrases = (
            "Не удалось создать новую запись. Прежнюю запись не отменял."
            if state.transfer.reason == "move_create_rejected"
            else "Перенос требует проверки сотрудником. Изменения сохранены в журнале.",
            "Запрос регистратору подготовлен.",
        )
    elif route == "R-01":
        phrases = (
            "Немедленно позвоните в скорую помощь: сто три или сто двенадцать.",
            "Не ждите приёма в поликлинике. Запрос регистратору подготовлен.",
        )
    elif route == "R-04":
        phrases = (load_phrasebook().get("transfer.medical").text,)
    elif route == "R-08" and state.transfer and state.transfer.reason == "paper_referral":
        phrases = (load_phrasebook().get("referral.paper").text,)
    else:
        phrases = ("Подготовлен запрос регистратору. Автоматическая запись остановлена.",)
    return reply(
        state,
        turn_id,
        *phrases,
        code="EMERGENCY"
        if route == "R-01"
        else "MIS_WAIT_EXPIRED"
        if route == "R-06" and state.transfer and state.transfer.reason == "mis_wait"
        else "MOVE_NEEDS_OPERATOR"
        if route == "R-06" and state.transfer and state.transfer.reason
        else "TRANSFER_REQUESTED",
    )


def preempt_state(state: DialogueState, transfer: TransferRequest) -> DialogueState:
    if state.move_proposal is not None:
        transfer = transfer.model_copy(update={"action_id": state.move_proposal.action_id})
    if state.cancel_proposal is not None:
        transfer = transfer.model_copy(update={"action_id": state.cancel_proposal.action_id})
    if state.proposal is not None:
        transfer = transfer.model_copy(update={"action_id": state.proposal.action_id})
    return state.model_copy(
        update={
            "phase": Phase.HANDOFF,
            "identity_draft": None,
            "version": state.version + 1,
            "last_turn_id": state.pending_turn_id or state.last_turn_id,
            "pending_turn_id": None,
            "authorization": None,
            "proposal": None,
            "cancel_proposal": None,
            "cancel_target": None,
            "move_proposal": None,
            "move_target": None,
            "options": (),
            "retry_at": None,
            "mis_wait_since": None,
            "transfer": transfer,
            "reminder_result": ReminderResult(
                attempt_id=state.reminder.binding.attempt_id, outcome="ESCALATED"
            )
            if state.reminder
            else None,
            "visit_items": (),
            "visit_cursor": None,
            "visit_has_more": False,
        }
    )
