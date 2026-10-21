"""Текстовый маршрутизатор: NLU Ильи, сценарии С-01/С-05 и правила действий backend."""

import hashlib
import json
import re
from datetime import datetime, time, timedelta
from typing import Literal, Mapping

from sqlalchemy.exc import SQLAlchemyError

from app.booking.models import ActionState, Approval, BookingError
from app.booking.preferences import PREFERENCE_FIELDS, SlotPreferences
from app.booking.service import BookingService
from app.cancellation.models import CancelState
from app.cancellation.service import CancellationService
from app.clock import CLINIC_TIMEZONE
from app.contracts import NluResult, SafetyDecision
from app.diagnostics.recorder import emit
from app.dialogue.cancellation import CancellationConversation
from app.dialogue.cross_scenario import CrossScenario
from app.dialogue.cross_scenario import classify as classify_cross_scenario
from app.dialogue.doctors import DoctorNames
from app.dialogue.failures import mis_failure
from app.dialogue.identity import IdentityDrafts
from app.dialogue.information import InformationConversation
from app.dialogue.medical import MedicalConversation
from app.dialogue.models import (
    DialogueState,
    Phase,
    Proposal,
    Reply,
    SuspendedBooking,
    TransferRequest,
    Turn,
    reply,
)
from app.dialogue.nlu_adapter import RulesInput
from app.dialogue.ports import DialogueStore, NluInterpreter, SafetyChecker
from app.dialogue.presentation import BookingPresenter
from app.dialogue.rescheduling import RescheduleConversation
from app.dialogue.safety_adapter import route_for
from app.dialogue.transitions import handoff_reply
from app.dialogue.triage import needs_urgent_screening
from app.dialogue.visit_privacy import mentions_existing_visits, requested_owner
from app.dialogue.visits import VisitConversation, wants_new_booking
from app.information.catalog import information_service
from app.mis.ports import MisError
from app.nlu.dictionaries import load_specialties
from app.nlu.parsers.answers import parse_answer
from app.policies.safety import operators_on_duty
from app.rescheduling.models import MoveState
from app.rescheduling.service import RescheduleService
from app.scheduling.matching import profile_key
from app.visits.models import VisitQuery
from app.visits.ports import DoctorRooms
from app.visits.service import VisitService
from app.voice.phrases import load_phrasebook
from app.voice.render import say_date, say_specialty
from app.waitlist.memory import MemoryWaitlistStore
from app.waitlist.models import WaitlistRequest
from app.waitlist.ports import WaitlistStore

FOLLOW_UP_QUESTION = "Могу ещё чем-то помочь?"


class BookingConversation:
    def __init__(
        self,
        booking: BookingService,
        store: DialogueStore,
        safety: SafetyChecker,
        *,
        nlu: NluInterpreter | None = None,
        rooms: DoctorRooms | None = None,
        cancellation: CancellationService | None = None,
        rescheduling: RescheduleService | None = None,
        presenter: BookingPresenter | None = None,
        identity_drafts: IdentityDrafts | None = None,
        waitlist_store: WaitlistStore | None = None,
        paid_prices: Mapping[str, int] | None = None,
    ) -> None:
        self.booking = booking
        self.store = store
        self.safety = safety
        self.handoffs = None
        self.identity_drafts = identity_drafts
        self.waitlist_store = waitlist_store or MemoryWaitlistStore()
        self.paid_prices = {profile_key(name): price for name, price in (paid_prices or {}).items()}
        if cancellation is not None and cancellation.operations is None:
            cancellation.operations = booking.operations
        self.rooms = rooms
        self.presenter = presenter if presenter is not None else BookingPresenter(booking.clock)
        self.information = InformationConversation(
            information_service(),
            self.presenter,
            store,
            self.information_continuation,
            booking.store.release_call_holds,
            self.transfer,
        )
        self.input = (
            nlu if nlu is not None else RulesInput(booking.mis, booking.clock, booking.schedule)
        )
        self.names = DoctorNames(booking.mis, booking.schedule)
        self.visits = VisitConversation(
            booking,
            store,
            VisitService(booking.mis, booking.clock, rooms),
            self.transfer,
            self.resume_booking_detour,
        )
        self.cancellation = (
            CancellationConversation(booking, cancellation, store, self.transfer)
            if cancellation is not None
            else None
        )
        self.rescheduling = (
            RescheduleConversation(rescheduling, store, self.transfer, rooms)
            if rescheduling
            else None
        )
        self.medical = MedicalConversation(
            booking,
            store,
            self.transfer,
            cancellation=cancellation,
            rescheduling=rescheduling,
            release=self.release_call,
        )

    async def release_call(self, call_id: str) -> None:
        await self.booking.store.release_call_holds(call_id)
        if self.cancellation is not None:
            await self.cancellation.cancellation.store.release_call(call_id)
        if self.rescheduling is not None:
            await self.rescheduling.service.store.release_call(call_id)

    async def resume_booking_detour(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply]:
        suspended = state.suspended_booking
        if suspended is None:
            return state, reply(state, turn.turn_id, "Продолжим. Чем могу помочь?")
        saved_phase = suspended.phase
        phase = "identified" if saved_phase in {"offered", "held"} else saved_phase
        state = state.model_copy(
            update={
                "purpose": "booking",
                "phase": Phase(phase),
                "specialty": suspended.specialty,
                "preferences": suspended.preferences,
                "identity_attempts": suspended.identity_attempts,
                "identity_draft": suspended.identity_draft,
                "misunderstandings": suspended.misunderstandings,
                "last_question": suspended.last_question,
                "referral_fallback": suspended.referral_fallback,
                "referral_mentioned": suspended.referral_mentioned,
                "suspended_booking": None,
                "authorization": None,
                "proposal": None,
                "options": (),
                "shown_option_keys": (),
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "query_for_self": False,
                "visit_query": VisitQuery(),
            }
        )
        if saved_phase in {"offered", "held"} and state.specialty:
            if state.client_id is None:
                state = state.model_copy(update={"phase": Phase.IDENTIFYING})
            else:
                state, result = await self.offer(state, turn)
                return state, result.model_copy(
                    update={"utterances": ("Вернёмся к подбору времени.", *result.utterances)}
                )
        if state.phase == Phase.IDENTIFYING:
            prompt = (
                "Назовите дату рождения, пожалуйста."
                if state.identity_draft
                else self.presenter.ask_patient()
            )
            return state, reply(
                state, turn.turn_id, "Вернёмся к записи.", prompt, code="BOOKING_RESUMED"
            )
        if state.specialty:
            return state, reply(
                state,
                turn.turn_id,
                "Вернёмся к записи.",
                "Скажите, на какой день хотите подобрать время?",
                code="BOOKING_RESUMED",
            )
        return state, reply(
            state,
            turn.turn_id,
            "Вернёмся к записи.",
            "К какому специалисту хотите записаться?",
            code="BOOKING_RESUMED",
        )

    async def short_reply(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply] | None:
        decision = self.safety.evaluate(state.call_id, turn)
        medical_followup = self.information.medical_followup(state, turn)
        if (
            not medical_followup
            and self.information.service.find(turn.text) == "vaccine_availability"
        ):
            informational = await self.information.step(state, turn)
            if informational is not None:
                return informational
        if decision.flags & {"medical", "robot", "no_recording"} or medical_followup:
            state = state.model_copy(
                update={"information_topic": None, "information_clarifying": False}
            )
        if "medical" in decision.flags or medical_followup:
            state, result = await self.medical.question(state, turn)
            return state, self.information.laboratory_boundary(
                result, turn, contextual=medical_followup
            )
        if decision.flags & {"robot", "no_recording"}:
            return await self.service_question(state, turn, decision.flags)
        if (
            self.cancellation is not None
            and state.purpose == "cancel"
            and state.phase == Phase.CANCEL_OFFER
            and parse_answer(turn.text) == "no"
        ):
            result = await self.input.parse(state, turn)
            return await self.cancellation.step(state, turn, result)
        if state.distress_question_pending:
            # Миграция старого активного диалога: не просим пациента оценивать
            # угрозу жизни, а передаём ситуацию сотруднику.
            return await self.transfer(state, turn, "R-08")
        if needs_urgent_screening(turn.text):
            return await self.transfer(state, turn, "R-08")
        if state.medical_boundary_pending and not state.information_clarifying:
            answer = self.information.short_answer(turn.text)
            if answer == "no":
                return await self.medical.decline(state, turn)
            if answer == "repeat":
                return self.medical.prompt(state, turn)
        if state.information_topic == "paid_price_followup":
            answer = parse_answer(turn.text)
            if answer == "no":
                ended = await self.store.end(state.call_id)
                return ended, reply(
                    ended,
                    turn.turn_id,
                    "Хорошо. Спасибо за звонок. До свидания.",
                    code="CALL_ENDED",
                )
            state = state.model_copy(update={"information_topic": None})
            if answer == "yes":
                return state, reply(
                    state, turn.turn_id, "Скажите, чем ещё могу помочь?", code="HELP_REQUESTED"
                )
        if state.information_topic == "paid_price":
            answer = parse_answer(turn.text)
            if answer == "no" or turn.text.casefold().lstrip().startswith("нет"):
                state = state.model_copy(update={"information_topic": "paid_price_followup"})
                return state, reply(
                    state, turn.turn_id, "Хорошо. Могу ещё чем-то помочь?", code="HELP_OFFERED"
                )
            if answer == "yes":
                return await self.transfer(state, turn, "R-10")
            return state, reply(
                state,
                turn.turn_id,
                "Соединить вас с платным отделением?",
                code="PAID_TRANSFER_REQUIRED",
            ).model_copy(update={"expect": "yes_no"})
        return await self.information.step(state, turn)

    async def service_question(
        self, state: DialogueState, turn: Turn, flags: frozenset[str]
    ) -> tuple[DialogueState, Reply]:
        """Служебный ответ не создаёт нового вопроса согласия и не продлевает бронь."""
        state = state.model_copy(update={"authorization": None})
        if self.rescheduling and state.move_proposal:
            action = await self.rescheduling.service.store.get(state.move_proposal.action_id)
            if action.state != MoveState.HELD:
                state = state.model_copy(update={"phase": Phase.MOVE_PENDING})
        phrases = []
        if "robot" in flags:
            phrases.append("Я виртуальный помощник регистратуры.")
        if "no_recording" in flags:
            state = state.model_copy(update={"recording_opt_out": True})
            phrases.append("Хорошо, запись разговора отключена.")
        continuation = (
            ("Чем могу помочь?",)
            if state.phase == Phase.READY and not state.medical_boundary_pending
            else self.information.resume_question(state, turn)
        )
        return state, reply(
            state,
            turn.turn_id,
            *phrases,
            *continuation,
            code="RECORDING_OPT_OUT" if "no_recording" in flags else "ROBOT_DISCLOSED",
        )

    async def reset_identity(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply | None]:
        """Новый поиск не наследует старого пациента даже при отказе/429 МИС."""
        pending = state.phase in {
            Phase.RECONCILING,
            Phase.CANCEL_RECONCILING,
            Phase.MOVE_PENDING,
            Phase.MOVE_FINISH_CONFIRM,
        }
        if state.proposal:
            action = await self.booking.store.get(state.proposal.action_id)
            pending |= action.state in {
                ActionState.COMMITTING,
                ActionState.RECONCILING,
                ActionState.BOOKED,
            }
        if self.cancellation and state.cancel_proposal:
            action = await self.cancellation.cancellation.store.get(state.cancel_proposal.action_id)
            pending |= action.state in {CancelState.COMMITTING, CancelState.RECONCILING}
        if self.rescheduling and state.move_proposal:
            action = await self.rescheduling.service.store.get(state.move_proposal.action_id)
            pending |= action.state in {
                MoveState.CREATING,
                MoveState.CREATE_UNKNOWN,
                MoveState.NEW_CREATED,
                MoveState.CANCELLING,
                MoveState.CANCEL_UNKNOWN,
                MoveState.MANUAL,
            }
        if pending:
            # Уже отправленное изменение остаётся в журнале T06. Новый пациент
            # не вправе продолжать его, а локальный сброс не отменяет запрос МИС.
            return await self.transfer(state, turn, "R-16")
        await self.release_call(state.call_id)
        self.booking.clear_call_cache(state.call_id)
        return state.model_copy(
            update={
                "client_id": None,
                "phase": Phase.IDENTIFYING,
                "proposal": None,
                "cancel_proposal": None,
                "move_proposal": None,
                "cancel_target": None,
                "move_target": None,
                "authorization": None,
                "options": (),
                "visit_items": (),
                "visit_cursor": None,
                "visit_has_more": False,
                "visit_query": VisitQuery(),
                "visit_id": None,
                "identity_draft": None,
                "identity_attempts": 0,
                "misunderstandings": 0,
                "retry_at": None,
                "mis_wait_since": None,
            }
        ), None

    def information_continuation(self, state: DialogueState, turn: Turn) -> tuple[str, ...]:
        """Повтор действующего вопроса только из локальных данных, без чтений МИС."""
        if state.phase == Phase.HELD and state.proposal is not None:
            room = (
                self.rooms.room(state.proposal.slot.doctor_id, state.proposal.slot.scheduled_at)
                if self.rooms
                else None
            )
            return self.presenter.confirmation(
                state.proposal.slot, state.proposal.doctor_name, room
            )
        if state.phase == Phase.CANCEL_CONFIRM and self.cancellation and state.cancel_proposal:
            return self.cancellation.confirmation(state, turn)[1].utterances
        if (
            state.phase in {Phase.MOVE_CONFIRM, Phase.MOVE_FINISH_CONFIRM}
            and self.rescheduling
            and state.move_proposal
        ):
            return self.rescheduling.confirmation(state, turn)[1].utterances
        if state.phase == Phase.CANCEL_OFFER and state.cancel_target:
            return (
                CancellationConversation.visit_phrase(state.cancel_target),
                "Могу перенести запись вместо отмены. Перенести или отменить?",
            )
        if state.phase == Phase.MOVE_SOURCE and state.move_target:
            return (
                CancellationConversation.visit_phrase(state.move_target),
                "Эту запись переносим? Укажите новый день или время.",
            )
        return ()

    async def transfer(
        self,
        state: DialogueState,
        turn: Turn,
        route: str,
        priority: Literal["P1", "P2", "P3"] = "P3",
        *,
        reason: Literal[
            "move_create_rejected",
            "move_manual",
            "mis_wait",
            "mis_unknown",
            "privacy",
            "paper_referral",
        ]
        | None = None,
    ) -> tuple[DialogueState, Reply]:
        if reason == "mis_unknown" and self.handoffs is not None:
            action_id = state.proposal.action_id if state.proposal else "unknown"
            await self.handoffs.ensure_request(
                state.call_id,
                route,
                priority,
                state,
                summary_override=(
                    f"Неизвестен результат записи; требуется сверка операции {action_id}"
                ),
            )
        state = await self.store.preempt(
            state.call_id,
            turn.turn_id,
            TransferRequest(route=route, priority=priority, reason=reason),
        )
        await self.release_call(state.call_id)
        return state, handoff_reply(state, turn.turn_id)

    @staticmethod
    def _catalog_route(text: str) -> str | None:
        """Resolve narrow destinations inside the otherwise broad I-08/I-14 classes."""
        value = text.casefold()
        if any(
            term in value
            for term in (
                "полиция",
                "звоню из организации",
                "звоню от организации",
                "это страховая",
                "представляю страховую",
                "прокуратур",
            )
        ):
            return "R-14"
        if any(
            term in value
            for term in (
                "персональн",
                "отозвать согласие",
                "информация о пациенте",
                "данные о пациенте",
            )
        ):
            return "R-16"
        if any(term in value for term in ("медкомисси", "медицинскую комисси", "по дмс", "дмс")):
            return "R-10"
        return None

    async def offer(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        await self.booking.store.release_call_holds(state.call_id)
        state = state.model_copy(
            update={
                "proposal": None,
                "authorization": None,
                "options": (),
                "phase": Phase.IDENTIFIED,
            }
        )
        try:
            options = await self.booking.options(
                state.client_id, state.specialty, state.preferences, limit=2, call_id=state.call_id
            )
            doctors = {d.id: d for d in await self.names.speaking()}
        except MisError as error:
            # Отменённая старая бронь и новые предпочтения должны сохраниться даже при ожидании.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError as error:
            if str(error) == "ELIGIBILITY_UNKNOWN":
                specialty = next(
                    (
                        item
                        for key, item in load_specialties().items()
                        if profile_key(key) == profile_key(state.specialty or "")
                    ),
                    None,
                )
                if specialty is not None and specialty.referral:
                    state = state.model_copy(
                        update={"information_topic": "referral_missing", "phase": Phase.IDENTIFIED}
                    )
                    message = (
                        load_phrasebook()
                        .get("referral.missing")
                        .text.replace("{specialty:datv}", say_specialty(state.specialty, "datv"))
                    )
                    return state, reply(
                        state, turn.turn_id, message, code="REFERRAL_REQUIRED"
                    ).model_copy(update={"expect": "yes_no"})
                return state, reply(
                    state,
                    turn.turn_id,
                    "В расписании есть время, но я не могу подтвердить условия записи к этому специалисту: прикрепление, направление или другой необходимый допуск.",
                    "Это нужно уточнить у регистратора. Можно выбрать другого специалиста или попросить оператора.",
                    code="ELIGIBILITY_UNKNOWN",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Подбор недоступен. Уточните данные или попросите регистратора.",
                code="BOOKING_REJECTED",
            )
        state = state.model_copy(
            update={"options": options, "phase": Phase.OFFERED if options else Phase.IDENTIFIED}
        )
        if not options:
            today = self.booking.clock.now().astimezone(CLINIC_TIMEZONE).date()
            requested_start = state.preferences.date_from
            requested_end = state.preferences.date_to
            if (requested_start is not None and requested_start > today + timedelta(days=13)) or (
                requested_end is not None and requested_end < today
            ):
                return state, reply(
                    state,
                    turn.turn_id,
                    "На эту дату запись пока недоступна. Можно выбрать дату в ближайшие две недели.",
                    code="NO_SLOTS",
                )
            specialty = next(
                (
                    item
                    for key, item in load_specialties().items()
                    if profile_key(key) == profile_key(state.specialty or "")
                ),
                None,
            )
            if specialty is not None and specialty.referral and not state.referral_mentioned:
                state = state.model_copy(update={"information_topic": "referral_missing"})
                message = (
                    load_phrasebook()
                    .get("referral.missing")
                    .text.replace("{specialty:datv}", say_specialty(state.specialty, "datv"))
                )
                return state, reply(
                    state, turn.turn_id, message, code="REFERRAL_REQUIRED"
                ).model_copy(update={"expect": "yes_no"})
            state = state.model_copy(
                update={"phase": Phase.WAITLIST_CONSENT, "waitlist_stage": "consent"}
            )
            offer_text = (
                load_phrasebook()
                .get("waitlist.offer")
                .text.replace("{specialty:datv}", say_specialty(state.specialty, "datv"))
            )
            return state, reply(
                state,
                turn.turn_id,
                offer_text,
                code="WAITLIST_CONSENT_REQUIRED",
            ).model_copy(update={"expect": "yes_no"})
        offered, phrases = self.presenter.offer(options, doctors)
        specialty = next(
            (
                item
                for key, item in load_specialties().items()
                if profile_key(key) == profile_key(state.specialty or "")
            ),
            None,
        )
        if (
            specialty is not None
            and specialty.referral
            and getattr(self.booking.schedule, "access_state", lambda *_: None)(
                state.client_id, options[0]
            )
            == "allowed"
        ):
            phrases = (
                f"Вижу направление к {say_specialty(state.specialty, 'datv')}.",
                *phrases,
            )
        state = state.model_copy(
            update={
                "options": offered,
                "shown_option_keys": tuple(self._slot_key(slot) for slot in offered),
            }
        )
        return state, reply(state, turn.turn_id, *phrases)

    @staticmethod
    def _slot_key(slot) -> str:
        return f"{slot.doctor_id}:{slot.scheduled_at.isoformat()}"

    async def cross_scenario_reply(
        self, state: DialogueState, turn: Turn, result: NluResult | None = None
    ) -> tuple[DialogueState, Reply] | None:
        if state.home_visit_stage is not None:
            return await self.home_visit_step(state, turn)
        text = turn.text.casefold()
        if "вызвать" in text and "врач" in text and "дом" in text:
            if self.handoffs is None:
                return await self.transfer(state, turn, "R-08")
            now = self.booking.clock.now()
            if now.time() < time(14):
                handed_state, result = await self.transfer(state, turn, "R-08")
                return handed_state, result.model_copy(
                    update={
                        "utterances": (
                            "Соединяю с регистратором, чтобы уточнить приём заявки на сегодня.",
                        )
                    }
                )
            next_day = now.date() + timedelta(days=1)
            state = state.model_copy(
                update={
                    "home_visit_stage": "confirm",
                    "home_visit_date": datetime.combine(next_day, time.min, tzinfo=now.tzinfo),
                }
            )
            phrase = (
                load_phrasebook()
                .get("home_visit.late")
                .text.replace(
                    "{next_day:short_accs}", say_date(next_day, "short_accs", today=now.date())
                )
            )
            return state, reply(state, turn.turn_id, phrase, code="HOME_VISIT_CONTINUE").model_copy(
                update={"expect": "yes_no"}
            )
        if "бумажн" in turn.text.casefold() and "направлен" in turn.text.casefold():
            return await self.transfer(state, turn, "R-08", reason="paper_referral")
        kind = classify_cross_scenario(turn.text)
        if kind is None:
            return None
        # These states have domain-owned retry behavior. Let cancellation,
        # rescheduling, visit refresh, and medical-boundary handlers process
        # a repeat before the generic cross-scenario shortcut.
        if kind == CrossScenario.REPEAT and (
            state.medical_boundary_pending
            or state.phase
            in {
                Phase.CANCEL_CHOOSING,
                Phase.CANCEL_OFFER,
                Phase.CANCEL_CONFIRM,
                Phase.CANCEL_RECONCILING,
                Phase.MOVE_CHOOSING,
                Phase.MOVE_CONFIRM,
                Phase.MOVE_FINISH_CONFIRM,
                Phase.MOVE_PENDING,
                Phase.VISITS_SHOWN,
            }
        ):
            return None
        if kind == CrossScenario.REPEAT and state.information_topic is not None:
            informational = await self.information.step(state, turn)
            if informational is not None:
                return informational
        if kind == CrossScenario.PRICE and state.phase not in {
            Phase.READY,
            Phase.BOOKED,
            Phase.RESCHEDULED,
            Phase.CANCELLED,
            Phase.ENDED,
        }:
            continuation = self.information.resume_question(state, turn)
            return state, reply(
                state,
                turn.turn_id,
                "Точную стоимость приёма не могу назвать; её уточнит регистратор.",
                *(continuation or ("Продолжим с текущего вопроса.",)),
                code="PRICE_UNAVAILABLE",
            )
        if kind == CrossScenario.REPEAT:
            if not state.last_question:
                return None
            return state, reply(
                state,
                turn.turn_id,
                state.last_question,
                code="QUESTION_REPEATED",
            )
        if (
            kind == CrossScenario.OTHER_DAY
            and state.purpose == "booking"
            and state.phase in {Phase.OFFERED, Phase.HELD}
        ):
            if state.phase == Phase.HELD and state.proposal is not None:
                action = await self.booking.store.get(state.proposal.action_id)
                if action.state != ActionState.HELD:
                    return None
                await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.IDENTIFIED,
                        "proposal": None,
                        "options": (),
                        "shown_option_keys": (),
                        "authorization": None,
                    }
                )
            return state, reply(
                state,
                turn.turn_id,
                "Конечно. Какой другой день вам удобен?",
                code="OTHER_DAY_REQUESTED",
            )
        if (
            kind == CrossScenario.OTHER_DAY
            and state.purpose == "reschedule"
            and state.phase
            in {
                Phase.IDENTIFYING,
                Phase.MOVE_CHOOSING,
                Phase.MOVE_SOURCE,
                Phase.MOVE_OFFERED,
                Phase.MOVE_CONFIRM,
            }
        ):
            if state.phase in {Phase.MOVE_OFFERED, Phase.MOVE_CONFIRM}:
                await self.rescheduling.service.store.release_call(state.call_id)
                state = state.model_copy(
                    update={
                        "phase": Phase.MOVE_SOURCE,
                        "move_proposal": None,
                        "options": (),
                        "authorization": None,
                    }
                )
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо. Какой новый день или время вам подходит?",
                code="OTHER_DAY_REQUESTED",
            )
        if kind == CrossScenario.WAIT:
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо, подожду. Скажите «продолжим», когда будете готовы.",
                code="WAIT_REQUESTED",
            )
        if kind == CrossScenario.GOODBYE:
            if state.phase in {
                Phase.HELD,
                Phase.CANCEL_CONFIRM,
                Phase.MOVE_CONFIRM,
                Phase.MOVE_FINISH_CONFIRM,
            }:
                state = state.model_copy(update={"end_confirmation_pending": True})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Перед завершением уточню: закончить разговор без выполнения этого действия?",
                    code="END_CONFIRMATION_REQUIRED",
                )
            await self.release_call(state.call_id)
            ended = await self.store.end(state.call_id)
            return ended, reply(
                ended, turn.turn_id, "Спасибо за обращение. До свидания.", code="CALL_ENDED"
            )
        if kind == CrossScenario.RESET:
            if state.phase in {
                Phase.RECONCILING,
                Phase.CANCEL_RECONCILING,
                Phase.MOVE_PENDING,
                Phase.MOVE_FINISH_CONFIRM,
            }:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Действие ещё проверяется. Повторно отправлять его не буду.",
                    "Скажите «проверьте результат» или попросите регистратора.",
                    code="ACTION_PENDING",
                )
            if state.reminder is not None:
                state = state.model_copy(update={"end_confirmation_pending": False})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Хорошо, начнём сначала: вы придёте на приём? Ответьте «да» или «нет».",
                    code="REMINDER_RESTART",
                )
            await self.release_call(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.READY,
                    "client_id": None,
                    "options": (),
                    "shown_option_keys": (),
                    "proposal": None,
                    "cancel_proposal": None,
                    "cancel_target": None,
                    "move_proposal": None,
                    "move_target": None,
                    "authorization": None,
                    "specialty": None,
                    "preferences": type(state.preferences)(),
                    "purpose": "booking",
                    "visit_id": None,
                    "identity_draft": None,
                    "identity_attempts": 0,
                    "visit_items": (),
                    "visit_cursor": None,
                    "visit_has_more": False,
                    "visit_query": VisitQuery(),
                    "query_for_self": False,
                    "cancel_reschedule_offered": False,
                    "information_topic": None,
                    "information_clarifying": False,
                    "medical_boundary_pending": False,
                    "misunderstandings": 0,
                    "end_confirmation_pending": False,
                    "retry_at": None,
                    "mis_wait_since": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Хорошо, начнём сначала. Чем могу помочь?",
                code="DIALOGUE_RESTARTED",
            )
        if kind == CrossScenario.MORE_OPTIONS:
            if state.phase == Phase.OFFERED and state.client_id is not None and state.specialty:
                excluded = frozenset(
                    (int(key.split(":", 1)[0]), datetime.fromisoformat(key.split(":", 1)[1]))
                    for key in state.shown_option_keys
                )
                try:
                    options = await self.booking.options(
                        state.client_id,
                        state.specialty,
                        state.preferences,
                        limit=2,
                        exclude=excluded,
                        call_id=state.call_id,
                    )
                except MisError as error:
                    return mis_failure(state, turn, self.booking.clock.now(), error)
                if options:
                    doctors = {d.id: d for d in await self.names.speaking()}
                    offered, phrases = self.presenter.offer(options, doctors)
                    state = state.model_copy(
                        update={
                            "options": offered,
                            "shown_option_keys": state.shown_option_keys
                            + tuple(self._slot_key(slot) for slot in offered),
                        }
                    )
                    return state, reply(state, turn.turn_id, *phrases, code="MORE_OPTIONS")
                return state, reply(
                    state,
                    turn.turn_id,
                    "Других подтверждённых вариантов пока нет. Назовите другой день или время.",
                    code="NO_MORE_OPTIONS",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Сначала уточним текущий вопрос. Если хотите, назовите другой день или время.",
                code="MORE_OPTIONS_CONTEXT",
            )
        if kind == CrossScenario.UNSURE:
            if state.phase in {Phase.HELD, Phase.CANCEL_CONFIRM, Phase.MOVE_CONFIRM}:
                if state.misunderstandings >= 1:
                    return await self.transfer(state, turn, "R-05")
                state = state.model_copy(update={"misunderstandings": 1, "authorization": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Не расслышала точный ответ. Подтверждаете действие? Ответьте «да» или «нет».",
                    code="CONFIRMATION_CLARIFICATION",
                )
            if state.reminder is not None:
                if state.misunderstandings >= 1:
                    return await self.transfer(state, turn, "R-05")
                state = state.model_copy(update={"misunderstandings": 1})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Вы придёте на приём? Ответьте, пожалуйста, точно: «да» или «нет».",
                    code="REMINDER_CLARIFICATION",
                )
            return state, reply(
                state,
                turn.turn_id,
                "Нужен точный ответ: да или нет?",
                code="YES_NO_CLARIFICATION",
            )
        if kind == CrossScenario.FILLER:
            question = self.information.resume_question(state, turn)
            return state, reply(
                state,
                turn.turn_id,
                "Не расслышала ответ.",
                *(question or ("Повторите, пожалуйста, ответ на текущий вопрос.",)),
                code="CONTEXT_REPROMPT",
            )
        if kind == CrossScenario.THIRD_PARTY:
            information_topic = self.information.service.find(turn.text)
            if (
                information_topic is not None
                and state.purpose not in {"visits", "cancel", "reschedule"}
                and not mentions_existing_visits(turn.text)
            ):
                return await self.information.step(state, turn)
            return await self.visits.privacy_transfer(state, turn)
        if kind == CrossScenario.SWITCH_CANCEL:
            if result is None and state.reminder is not None:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Сейчас идёт звонок-напоминание. Изменить запись в нём нельзя.",
                    "Для отмены обратитесь в регистратуру.",
                    code="REMINDER_SCOPE",
                )
            if state.purpose == "cancel" and state.phase == Phase.CANCEL_OFFER:
                return None
            if result is None or self.cancellation is None:
                return None
            return await self.cancellation.start(state, turn, result)
        if kind == CrossScenario.SWITCH_VISITS:
            if result is None:
                if state.reminder is not None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Сейчас идёт звонок-напоминание. Сведения о других записях в нём не проверяю.",
                        "Обратитесь в регистратуру, если хотите проверить свои записи.",
                        code="REMINDER_SCOPE",
                    )
                return None
            if (
                state.purpose == "booking"
                and state.suspended_booking is None
                and state.phase in {Phase.IDENTIFYING, Phase.IDENTIFIED, Phase.OFFERED, Phase.HELD}
            ):
                saved_phase = "offered" if state.phase == Phase.HELD else state.phase.value
                if state.phase == Phase.HELD:
                    await self.booking.store.release_call_holds(state.call_id)
                state = state.model_copy(
                    update={
                        "suspended_booking": SuspendedBooking(
                            phase=saved_phase,
                            specialty=state.specialty,
                            preferences=state.preferences,
                            identity_attempts=state.identity_attempts,
                            identity_draft=state.identity_draft,
                            misunderstandings=state.misunderstandings,
                            last_question=state.last_question,
                            referral_fallback=state.referral_fallback,
                            referral_mentioned=state.referral_mentioned,
                        ),
                        "phase": Phase.IDENTIFIED if state.phase == Phase.HELD else state.phase,
                        "proposal": None,
                        "authorization": None,
                        "options": () if state.phase == Phase.HELD else state.options,
                    }
                )
            return await self.visits.start(state, turn, result)
        return None

    async def home_visit_step(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply]:
        stage = state.home_visit_stage
        answer = parse_answer(turn.text)
        if stage == "confirm":
            if answer == "no":
                state = state.model_copy(update={"phase": Phase.ENDED, "home_visit_stage": None})
                return state, reply(
                    state, turn.turn_id, "Хорошо. Всего доброго.", code="CALL_ENDED"
                )
            if answer != "yes":
                return state, reply(
                    state, turn.turn_id, "Оформить вызов на завтра?", code="HOME_VISIT_CONFIRM"
                ).model_copy(update={"expect": "yes_no"})
            state = state.model_copy(
                update={"home_visit_stage": "identify", "phase": Phase.IDENTIFYING}
            )
            phrases = (
                load_phrasebook().get("home_visit.safety").text,
                self.presenter.ask_patient(),
            )
            return state, reply(state, turn.turn_id, *phrases, code="HOME_VISIT_IDENTITY")
        if stage == "identify":
            parsed = await self.input.parse(state, turn)
            slots = parsed.slots
            if not (slots.last_name and slots.first_name and slots.birth_date):
                return state, reply(
                    state,
                    turn.turn_id,
                    self.presenter.ask_patient(),
                    code="IDENTIFICATION_REQUIRED",
                )
            patient = await self.booking.identify(
                slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
            )
            state = state.model_copy(
                update={
                    "client_id": patient.id,
                    "phase": Phase.IDENTIFIED,
                    "home_visit_stage": "address",
                }
            )
            phrase = load_phrasebook().get("home_visit.ask_address").text.replace("{patient}, ", "")
            return state, reply(state, turn.turn_id, phrase, code="HOME_VISIT_ADDRESS").model_copy(
                update={"expect": "free"}
            )
        if stage in {"address", "reason"}:
            if self.handoffs is None:
                return await self.transfer(state, turn, "R-08")
            payload = (
                json.loads(
                    self.handoffs.cipher.decrypt(state.home_visit_payload_ciphertext.encode())
                )
                if state.home_visit_payload_ciphertext
                else {}
            )
            payload["address" if stage == "address" else "reason"] = turn.text.strip()
            ciphertext = self.handoffs.encrypt_home_visit_payload(payload)
            next_stage = "reason" if stage == "address" else "can_come"
            state = state.model_copy(
                update={"home_visit_stage": next_stage, "home_visit_payload_ciphertext": ciphertext}
            )
            key = "home_visit.ask_reason" if stage == "address" else "home_visit.ask_can_come"
            return state, reply(
                state, turn.turn_id, load_phrasebook().get(key).text, code="HOME_VISIT_QUESTION"
            ).model_copy(update={"expect": "free" if next_stage == "reason" else "yes_no"})
        if stage == "can_come":
            if answer == "yes":
                return state, reply(
                    state,
                    turn.turn_id,
                    load_phrasebook().get("home_visit.come_yourself").text,
                    code="HOME_VISIT_SELF_VISIT",
                )
            if answer != "no":
                return state, reply(
                    state,
                    turn.turn_id,
                    load_phrasebook().get("home_visit.ask_can_come").text,
                    code="HOME_VISIT_QUESTION",
                ).model_copy(update={"expect": "yes_no"})
            if self.handoffs is None or state.home_visit_payload_ciphertext is None:
                return await self.transfer(state, turn, "R-08")
            payload = json.loads(
                self.handoffs.cipher.decrypt(state.home_visit_payload_ciphertext.encode())
            )
            payload["requested_date"] = state.home_visit_date.date().isoformat()
            ciphertext = self.handoffs.encrypt_home_visit_payload(payload)
            state = state.model_copy(
                update={"home_visit_stage": "contact", "home_visit_payload_ciphertext": ciphertext}
            )
            request = await self.handoffs.create_home_visit_request(
                state.call_id, state, ciphertext
            )
            state = state.model_copy(update={"home_visit_request_id": request.request_id})
            phrase = (
                load_phrasebook()
                .get("home_visit.accepted")
                .text.replace(
                    "{date:short_accs}",
                    say_date(
                        state.home_visit_date.date(),
                        "short_accs",
                        today=self.booking.clock.now().date(),
                    ),
                )
            )
            return state, reply(
                state, turn.turn_id, phrase, code="HOME_VISIT_REQUESTED"
            ).model_copy(update={"expect": "yes_no"})
        if stage == "contact":
            if answer == "yes":
                state = state.model_copy(
                    update={
                        "phase": Phase.ENDED,
                        "home_visit_stage": None,
                        "home_visit_date": None,
                        "home_visit_payload_ciphertext": None,
                    }
                )
                return state, reply(
                    state,
                    turn.turn_id,
                    load_phrasebook().get("home_visit.done").text,
                    code="HOME_VISIT_REQUESTED",
                )
            return await self.transfer(state, turn, "R-08")
        return await self.transfer(state, turn, "R-08")

    async def _distress_triage(self, state: DialogueState, turn: Turn):
        # Неразмеченные симптомы нельзя превращать в медицинский триаж.
        # Явные красные флаги уже маршрутизированы safety-компонентом.
        return await self.transfer(state, turn, "R-08")

    async def step(self, state: DialogueState, turn: Turn) -> tuple[DialogueState, Reply]:
        # Новая предметная просьба завершает справочное уточнение; её данные не теряются.
        if state.information_topic != "referral_missing":
            state = state.model_copy(
                update={"information_topic": None, "information_clarifying": False}
            )
        if needs_urgent_screening(turn.text):
            return await self._distress_triage(state, turn)
        result = await self.input.parse(state, turn)
        if result.call_id != state.call_id or result.turn_id != turn.turn_id:
            raise RuntimeError("результат NLU не относится к текущей реплике")
        emit("nlu.parsed", **result.model_dump(mode="json", exclude={"call_id", "turn_id"}))
        decision = self.safety.evaluate(state.call_id, turn, result.flags)
        transfer = route_for(decision)
        if transfer:
            return await self.transfer(state, turn, transfer.route, transfer.priority)
        if state.distress_question_pending:
            return await self.transfer(state, turn, "R-08")
        if needs_urgent_screening(turn.text):
            return await self._distress_triage(state, turn)
        if self.information.service.find(turn.text) == "vaccine_availability":
            informational = await self.information.step(state, turn)
            if informational is not None:
                return informational
        if "medical" in decision.flags or "I-13" in result.intents:
            return await self.medical.question(state, turn)
        if decision.flags & {"robot", "no_recording"}:
            return await self.service_question(state, turn, decision.flags)
        if (
            state.guardian_call
            and state.patient_under_15
            and state.client_id is not None
            and state.guardian_relation is None
            and re.search(
                r"\b(это\s+я|я\s+(сам|сама)|самостоятельно)\b",
                turn.text.casefold().replace("ё", "е"),
            )
        ):
            return state, reply(
                state,
                turn.turn_id,
                self.presenter.renderer.render("ident.under_15", self.presenter._today()).text,
                code="MINOR_BOOKING_REFUSED",
            )
        cross_reply = await self.cross_scenario_reply(state, turn, result)
        if cross_reply is not None:
            return cross_reply
        informational = await self.information.step(state, turn)
        if informational is not None:
            return informational
        if state.client_id is not None and (
            any(
                getattr(result.slots, field) is not None
                for field in ("last_name", "first_name", "middle_name", "birth_date")
            )
            or result.alternatives
        ):
            state, stopped = await self.reset_identity(state, turn)
            if stopped is not None:
                return state, stopped
        try:
            return await self.advance(state, turn, result, decision)
        except MisError as error:
            # Внешний _process видит состояние до входа в step. Ошибка нового
            # поиска должна сохранить очищенную личность, а не старую бронь.
            return mis_failure(state, turn, self.booking.clock.now(), error)
        except BookingError as error:
            if str(error) != "IDENTIFICATION_REQUIRED":
                raise
            attempts = state.identity_attempts + 1
            state = state.model_copy(update={"identity_attempts": attempts, "identity_draft": None})
            prompt = (
                load_phrasebook().get("ident.ask_policy").text
                if attempts >= 2
                else "Не удалось однозначно найти пациента. Назовите ФИО и дату рождения ещё раз."
            )
            return state, reply(
                state,
                turn.turn_id,
                prompt,
                code="IDENTIFICATION_REQUIRED",
            )

    async def distress_answer(self, state: DialogueState, turn: Turn):
        answer = parse_answer(turn.text)
        if answer == "yes":
            await self.release_call(state.call_id)
            ended = state.model_copy(
                update={
                    "phase": Phase.ENDED,
                    "distress_question_pending": False,
                    "identity_draft": None,
                    "authorization": None,
                }
            )
            return ended, reply(
                ended,
                turn.turn_id,
                "Позвоните 103 или 112 сейчас. Я не могу вызвать скорую этим звонком.",
                code="EMERGENCY_GUIDANCE",
            )
        if answer == "no":
            state = state.model_copy(
                update={"distress_question_pending": False, "misunderstandings": 0}
            )
            return await self.medical.question(state, turn, after_triage=True)
        if state.misunderstandings == 0:
            state = state.model_copy(update={"misunderstandings": 1, "authorization": None})
            return state, reply(
                state,
                turn.turn_id,
                "Не удалось понять ответ. Если угроза жизни есть — скажите «да»; если нет — «нет».",
                "При опасном или быстро ухудшающемся состоянии позвоните 103 или 112.",
                code="DISTRESS_TRIAGE_CLARIFICATION",
            ).model_copy(update={"expect": "yes_no"})
        state = state.model_copy(
            update={"distress_question_pending": False, "misunderstandings": 0}
        )
        return await self.medical.question(state, turn)

    async def waitlist_preferences_question(
        self, state: DialogueState, turn: Turn
    ) -> tuple[DialogueState, Reply]:
        state = state.model_copy(
            update={"phase": Phase.WAITLIST_PREFERENCES, "waitlist_stage": "preferences"}
        )
        return state, reply(
            state,
            turn.turn_id,
            "В какие дни и время вам удобно прийти, если освободится место?",
            code="WAITLIST_PREFERENCES_REQUIRED",
        )

    async def waitlist_turn(self, state, turn, slots, intents):
        stage = state.waitlist_stage
        if stage == "specialty":
            if slots.specialty is None:
                return state, reply(
                    state, turn.turn_id, "Назовите специальность врача, пожалуйста."
                )
            state = state.model_copy(
                update={
                    "specialty": slots.specialty,
                    "phase": Phase.IDENTIFYING if state.client_id is None else Phase.IDENTIFIED,
                    "waitlist_stage": "identity" if state.client_id is None else "preferences",
                }
            )
            if state.client_id is None:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите фамилию, имя, отчество и дату рождения пациента.",
                    code="IDENTIFICATION_REQUIRED",
                )
            return await self.waitlist_preferences_question(state, turn)
        if stage == "consent":
            if "yes" in intents:
                if state.client_id is None:
                    state = state.model_copy(
                        update={"phase": Phase.IDENTIFYING, "waitlist_stage": "identity"}
                    )
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Назовите фамилию, имя, отчество и дату рождения пациента.",
                        code="IDENTIFICATION_REQUIRED",
                    )
                return await self.waitlist_preferences_question(state, turn)
            if "no" in intents:
                state = state.model_copy(update={"phase": Phase.IDENTIFIED, "waitlist_stage": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Хорошо, заявку создавать не буду. Могу подобрать другой вариант или соединить с регистратурой.",
                    code="WAITLIST_DECLINED",
                )
            return state, reply(
                state, turn.turn_id, "Оставить заявку в листе ожидания? Ответьте «да» или «нет»."
            )
        if stage == "identity":
            if not (slots.last_name and slots.first_name and slots.birth_date):
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите полные ФИО и дату рождения пациента.",
                    code="IDENTIFICATION_REQUIRED",
                )
            try:
                patient = await self.booking.identify(
                    slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
                )
            except MisError as error:
                return mis_failure(state, turn, self.booking.clock.now(), error)
            except BookingError:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Пациента не удалось однозначно найти. Повторите полные ФИО и дату рождения.",
                    code="IDENTIFICATION_REQUIRED",
                )
            state = state.model_copy(
                update={
                    "client_id": patient.id,
                    "identity_attempts": 0,
                    "waitlist_stage": "preferences",
                }
            )
            return await self.waitlist_preferences_question(state, turn)
        if stage == "preferences":
            fields = PREFERENCE_FIELDS & slots.model_fields_set
            if not fields:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Уточните удобные дни и время, например: «в любой день утром».",
                )
            preferences = state.preferences.model_copy(
                update={field: getattr(slots, field) for field in fields}
            )
            request_id = hashlib.sha256(
                f"waitlist\0{state.call_id}\0{turn.turn_id}".encode("utf-8")
            ).hexdigest()
            request = WaitlistRequest(
                request_id=request_id,
                call_id=state.call_id,
                turn_id=turn.turn_id,
                client_id=state.client_id,
                specialty=state.specialty,
                preferences=preferences,
                created_at=self.booking.clock.now(),
            )
            try:
                await self.waitlist_store.create(request)
            except (OSError, ValueError, SQLAlchemyError):
                return await self.transfer(state, turn, "R-08")
            state = state.model_copy(
                update={
                    "phase": Phase.IDENTIFIED,
                    "waitlist_stage": None,
                    "preferences": preferences,
                    "authorization": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                load_phrasebook().get("waitlist.done").text,
                code="WAITLISTED",
            )
        return await self.transfer(state, turn, "R-08")

    async def advance(
        self, state: DialogueState, turn: Turn, result: NluResult, decision: SafetyDecision
    ) -> tuple[DialogueState, Reply]:
        slots, intents = result.slots, set(result.intents)
        if state.waitlist_stage is not None:
            return await self.waitlist_turn(state, turn, slots, intents)
        if "I-09" in intents:
            if state.phase in {Phase.RECONCILING, Phase.CANCEL_RECONCILING, Phase.MOVE_PENDING}:
                return await self.transfer(state, turn, "R-06", "P3", reason="mis_unknown")
            state = state.model_copy(
                update={
                    "purpose": "booking",
                    "phase": Phase.IDENTIFYING if state.client_id is None else Phase.IDENTIFIED,
                    "specialty": slots.specialty,
                    "preferences": SlotPreferences(),
                    "options": (),
                    "proposal": None,
                    "authorization": None,
                    "waitlist_stage": "identity" if slots.specialty else "specialty",
                    "referral_mentioned": False,
                    "visit_items": (),
                    "suspended_booking": None,
                }
            )
            if state.waitlist_stage == "specialty":
                return state, reply(
                    state, turn.turn_id, "К какому специалисту поставить вас в лист ожидания?"
                )
            if state.client_id is None:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите фамилию, имя, отчество и дату рождения пациента.",
                    code="IDENTIFICATION_REQUIRED",
                )
            return await self.waitlist_preferences_question(state, turn)
        if state.information_topic == "referral_missing":
            if "yes" in intents:
                state = state.model_copy(
                    update={
                        "information_topic": None,
                        "referral_fallback": True,
                        "phase": Phase.IDENTIFIED,
                        "specialty": "терапевт",
                        "preferences": SlotPreferences(),
                    }
                )
                return await self.offer(state, turn)
            if "no" in intents:
                state = await self.store.end(state.call_id)
                return state, reply(
                    state, turn.turn_id, "Хорошо. Всего доброго.", code="CALL_ENDED"
                )
        if state.medical_boundary_pending:
            refinements = PREFERENCE_FIELDS & slots.model_fields_set
            if "repeat" in intents:
                return self.medical.prompt(state, turn)
            if "no" in intents and not ("I-01" in intents or slots.specialty):
                return await self.medical.decline(state, turn)
            # Уточнение дня начинает подбор, но не служит подтверждением записи.
            if "yes" in intents or (refinements and not intents & {"no", "maybe", "opt_out"}):
                if state.phase in {
                    Phase.RECONCILING,
                    Phase.CANCEL_RECONCILING,
                    Phase.MOVE_PENDING,
                    Phase.MOVE_FINISH_CONFIRM,
                }:
                    return await self.transfer(state, turn, "R-08")
                specialty = slots.specialty or state.specialty or "терапевт"
                state = self.visits.booking_state(state).model_copy(
                    update={"medical_boundary_pending": False, "specialty": specialty}
                )
                slots = slots.model_copy(update={"specialty": specialty})
                intents.add("I-01")
            elif intents & {"I-01", "I-02", "I-04"} or slots.specialty or slots.doctor:
                state = state.model_copy(update={"medical_boundary_pending": False})
            elif refinements:
                return self.medical.prompt(state, turn)
        child_booking_request = (
            state.purpose == "booking"
            and ("I-01" in intents or slots.specialty is not None)
            and (
                slots.on_behalf == "child"
                or re.search(
                    r"\b(доч\w*|сын\w*|реб[её]н\w*|несовершеннолетн\w*)\b",
                    turn.text.casefold(),
                )
                is not None
            )
        )
        if child_booking_request:
            state = state.model_copy(update={"guardian_call": True})
        if (
            requested_owner(turn.text) == "other"
            and (
                state.purpose in {"visits", "cancel", "reschedule"}
                or intents & {"I-02", "I-03", "I-04"}
                or mentions_existing_visits(turn.text)
            )
        ):
            return await self.visits.privacy_transfer(state, turn)
        if state.guardian_call and state.client_id is not None and state.guardian_relation is None:
            relation = re.search(
                r"\b(мам\w*|мат\w*|пап\w*|отц\w*|родител\w*|опекун\w*|попечител\w*|"
                r"бабушк\w*|дедушк\w*)\b",
                turn.text.casefold().replace("ё", "е"),
            )
            if relation is None:
                return state, reply(
                    state,
                    turn.turn_id,
                    self.presenter.renderer.render("ident.relation", self.presenter._today()).text,
                    code="GUARDIAN_RELATION_REQUIRED",
                )
            state = state.model_copy(update={"guardian_relation": relation.group(1)})
            if state.specialty:
                return await self.offer(state, turn)
        # Сохраняем только зашифрованное ФИО. Общий шаг работает для записи, справки,
        # отмены и переноса; окончательная проверка пациента остаётся строгой.
        if state.phase == Phase.IDENTIFYING and self.identity_drafts is not None:
            has_name = bool(slots.last_name and slots.first_name)
            if (slots.last_name or slots.first_name or slots.middle_name) and not has_name:
                state = state.model_copy(update={"identity_draft": None})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Назовите фамилию, имя и отчество полностью.",
                    code="IDENTIFICATION_REQUIRED",
                )
            if has_name:
                state = state.model_copy(
                    update={
                        "identity_draft": self.identity_drafts.seal(
                            state.call_id, slots.last_name, slots.first_name, slots.middle_name
                        )
                    }
                )
            if slots.birth_date and not result.alternatives and state.identity_draft:
                names = self.identity_drafts.open(state.call_id, state.identity_draft)
                state = state.model_copy(update={"identity_draft": None})
                if names is None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Назовите фамилию, имя, отчество и дату рождения ещё раз.",
                        code="IDENTIFICATION_REQUIRED",
                    )
                slots = slots.model_copy(update=names)
                result = result.model_copy(update={"slots": slots})
            elif has_name and not slots.birth_date:
                state = state.model_copy(update={"misunderstandings": 0})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Спасибо. Назовите дату рождения: день, месяц и год.",
                    code="BIRTH_DATE_REQUIRED",
                )
        if self.rescheduling and (state.purpose == "reschedule" or "I-02" in intents):
            sent = False
            if state.move_proposal:
                known = await self.rescheduling.service.store.get(state.move_proposal.action_id)
                sent = known.state not in {MoveState.HELD, MoveState.ABORTED, MoveState.EXPIRED}
            if state.phase == Phase.MOVE_EMPTY and (
                "yes" in intents or "I-01" in intents or slots.specialty
            ):
                state = self.visits.booking_state(state)
                if slots.specialty is None:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "К какому врачу хотите записаться?",
                        code="SPECIALTY_REQUIRED",
                    )
                intents.add("I-01")
            elif state.purpose == "reschedule" and (
                "I-03" in intents or "I-01" in intents and wants_new_booking(turn.text)
            ):
                if sent:
                    return await self.transfer(state, turn, "R-08")
                await self.rescheduling.service.store.release_call(state.call_id)
                state = self.visits.booking_state(state).model_copy(
                    update={"move_proposal": None, "move_target": None}
                )
            elif (
                state.purpose == "reschedule"
                and "I-04" in intents
                and state.phase == Phase.RESCHEDULED
            ):
                return await self.visits.start(state, turn, result)
            else:
                try:
                    if (
                        state.purpose != "reschedule"
                        or state.move_target is None
                        and state.move_proposal is None
                        and state.phase
                        not in {Phase.IDENTIFYING, Phase.MOVE_CHOOSING, Phase.MOVE_EMPTY}
                    ):
                        await self.release_call(state.call_id)
                        return await self.rescheduling.start(state, turn, result)
                    return await self.rescheduling.step(state, turn, result)
                except BookingError as error:
                    if str(error) in {"ACTION_PENDING", "CANCELLATION_BUSY"}:
                        return await self.transfer(state, turn, "R-08")
                    if state.move_proposal:
                        action = await self.rescheduling.service.store.get(
                            state.move_proposal.action_id
                        )
                        if action.state not in {
                            MoveState.HELD,
                            MoveState.ABORTED,
                            MoveState.EXPIRED,
                        }:
                            if str(error) == "CONFIRMATION_INTERRUPTED":
                                raise
                            return await self.transfer(state, turn, "R-06")
                    if str(error) in {"VISIT_CHANGED", "VISIT_PAST", "HOLD_NOT_ACTIVE"}:
                        await self.rescheduling.service.store.release_call(state.call_id)
                        state = state.model_copy(
                            update={
                                "move_proposal": None,
                                "move_target": None,
                                "options": (),
                                "phase": Phase.MOVE_CHOOSING,
                            }
                        )
                        return state, reply(
                            state,
                            turn.turn_id,
                            "Прежняя запись изменилась. Уточните запись для переноса.",
                            code="VISIT_CHANGED",
                        )
                    raise
        if self.cancellation is not None and (state.purpose == "cancel" or "I-03" in intents):
            if (
                state.purpose == "cancel"
                and state.phase == Phase.CANCEL_OFFER
                and "I-02" in intents
                and self.rescheduling is not None
            ):
                return await self.rescheduling.start(state, turn, result)
            if (
                state.purpose == "cancel"
                and "I-04" in intents
                and "I-03" not in intents
                and state.phase != Phase.CANCEL_RECONCILING
            ):
                await self.cancellation.cancellation.store.release_call(state.call_id)
                state = state.model_copy(update={"cancel_proposal": None, "cancel_target": None})
                return await self.visits.start(state, turn, result)
            if (
                state.purpose == "cancel"
                and "I-01" in intents
                and state.phase != Phase.CANCEL_RECONCILING
            ):
                await self.cancellation.cancellation.store.release_call(state.call_id)
                state = self.visits.booking_state(state).model_copy(
                    update={"cancel_proposal": None, "cancel_target": None}
                )
            else:
                try:
                    if state.purpose != "cancel":
                        return await self.cancellation.start(state, turn, result)
                    return await self.cancellation.step(state, turn, result)
                except BookingError as error:
                    if str(error) in {"ACTION_PENDING", "CANCELLATION_BUSY"}:
                        return await self.transfer(state, turn, "R-08")
                    if str(error) in {"VISIT_CHANGED", "VISIT_PAST", "HOLD_NOT_ACTIVE"}:
                        await self.cancellation.cancellation.store.release_call(state.call_id)
                        state = state.model_copy(
                            update={
                                "phase": Phase.CANCEL_CHOOSING,
                                "cancel_proposal": None,
                                "cancel_target": None,
                                "visit_items": (),
                                "authorization": None,
                            }
                        )
                        return state, reply(
                            state,
                            turn.turn_id,
                            "Данные записи изменились или подтверждение истекло. Скажите «повторите», чтобы получить актуальные записи.",
                            code="VISIT_CHANGED",
                        )
                    raise
        if state.purpose == "visits":
            if "I-01" in intents and (
                state.phase != Phase.VISITS_SHOWN
                or not state.visit_items
                or wants_new_booking(turn.text)
            ):
                state = self.visits.booking_state(state)
            elif intents & {"I-02", "I-03"}:
                return await self.transfer(state, turn, "R-08")
            else:
                return await self.visits.step(state, turn, result)
        if "I-04" in intents:
            if state.proposal is not None:
                known = await self.booking.store.get(state.proposal.action_id)
                if known.state == ActionState.HELD:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Запись пока не создана. Подтвердите выбранный приём.",
                        code="CONFIRMATION_REQUIRED",
                    )
                action = await self.booking.reconcile(state.proposal.action_id)
                if action.state == ActionState.BOOKED:
                    confirmed = self.booked_phrase(action, state.proposal.doctor_name)
                    referral_done = self._referral_done(state)
                    state = state.model_copy(
                        update={
                            "phase": Phase.BOOKED,
                            "visit_id": action.visit_id,
                            "proposal": None,
                            "options": (),
                        }
                    )
                    return state, reply(
                        state,
                        turn.turn_id,
                        confirmed,
                        *(referral_done or (FOLLOW_UP_QUESTION,)),
                        code="BOOKED_GP" if state.referral_fallback else "BOOKED",
                    )
                if action.state == ActionState.RECONCILING and operators_on_duty(
                    self.booking.clock.now().astimezone(CLINIC_TIMEZONE)
                ):
                    return await self.transfer(state, turn, "R-06", "P3", reason="mis_unknown")
                state = state.model_copy(update={"phase": Phase.RECONCILING})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Результат всё ещё уточняется. Повторную запись не отправляю.",
                    code="RECONCILING",
                )
            return await self.visits.start(state, turn, result)
        if state.phase == Phase.RECONCILING:
            if "no" in intents or intents & {"I-01", "I-02", "I-03"}:
                # Уже отправленная операция не отменяется местным снятием брони.
                return await self.transfer(state, turn, "R-06", "P3", reason="mis_unknown")
            return state, reply(
                state,
                turn.turn_id,
                "Результат ещё уточняется. Для сверки спросите: на когда я записана?",
                code="RECONCILING",
            )
        if state.phase == Phase.BOOKED and not slots.model_fields_set:
            if intents == {"yes"}:
                state = self.visits.booking_state(state).model_copy(
                    update={"referral_fallback": False}
                )
                return state, reply(
                    state, turn.turn_id, "Чем ещё могу помочь?", code="MORE_HELP_REQUIRED"
                )
            if intents == {"no"}:
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            if intents == {"repeat"}:
                return state, reply(
                    state,
                    turn.turn_id,
                    "Запись уже подтверждена.",
                    FOLLOW_UP_QUESTION,
                    code="MORE_HELP_OFFERED",
                )
        subroute = self._catalog_route(turn.text)
        if subroute is not None:
            return await self.transfer(state, turn, subroute)
        if "I-08" in intents:
            service = slots.service
            price = self.paid_prices.get(profile_key(service or ""))
            if service and price is not None:
                state = state.model_copy(update={"information_topic": "paid_price"})
                phrase = self.presenter.renderer.render(
                    "paid.price", self.presenter._today(), service=service, price=price
                ).text
                return state, reply(state, turn.turn_id, phrase, code="INFO_GIVEN").model_copy(
                    update={"expect": "yes_no"}
                )
            # I-08 without a configured price source goes to the paid department.
            return await self.transfer(state, turn, "R-10")
        if intents & {
            "I-02",
            "I-03",
            "I-05",
            "I-06",
            "I-07",
            "I-08",
            "I-09",
            "I-10",
            "I-12",
            "I-13",
            "I-14",
        }:
            return await self.transfer(state, turn, "R-08")
        if slots.on_behalf == "other" or (slots.on_behalf == "child" and not child_booking_request):
            return await self.transfer(state, turn, "R-16")
        if slots.policy_number is not None:
            return await self.transfer(state, turn, "R-07")
        if result.alternatives:
            await self.booking.store.release_call_holds(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.IDENTIFYING,
                    "client_id": None,
                    "proposal": None,
                    "options": (),
                    "authorization": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "Дата рождения неоднозначна. Назовите ФИО и дату рождения с четырёхзначным годом.",
                code="BIRTH_DATE_AMBIGUOUS",
            )
        if slots.last_name and slots.first_name and slots.birth_date:
            patient = await self.booking.identify(
                slots.last_name, slots.first_name, slots.middle_name, slots.birth_date
            )
            today = self.presenter._today()
            age = (
                today.year
                - patient.birth_date.year
                - ((today.month, today.day) < (patient.birth_date.month, patient.birth_date.day))
            )
            state = state.model_copy(
                update={
                    "client_id": patient.id,
                    "phase": Phase.IDENTIFIED,
                    "misunderstandings": 0,
                    "patient_under_15": age < 15,
                    "guardian_call": state.guardian_call or age < 15,
                }
            )
            if state.guardian_call:
                return state, reply(
                    state,
                    turn.turn_id,
                    self.presenter.renderer.render("ident.relation", self.presenter._today()).text,
                    code="GUARDIAN_RELATION_REQUIRED",
                )
            if state.specialty:
                return await self.offer(state, turn)
            return state, reply(
                state, turn.turn_id, "Пациент найден. К какому специалисту хотите записаться?"
            )
        preference_fields = PREFERENCE_FIELDS & slots.model_fields_set
        if (
            "I-01" in intents
            and slots.specialty is None
            and slots.doctor is None
            and state.phase in {Phase.READY, Phase.IDENTIFIED}
        ):
            state = state.model_copy(
                update={
                    "purpose": "booking",
                    "phase": Phase.IDENTIFIED if state.client_id is not None else Phase.READY,
                    "preferences": state.preferences.model_copy(
                        update={
                            field: getattr(slots, field)
                            for field in PREFERENCE_FIELDS & slots.model_fields_set
                        }
                    ),
                    "authorization": None,
                }
            )
            return state, reply(
                state,
                turn.turn_id,
                "К какому специалисту вы хотите записаться?",
                code="SPECIALTY_REQUIRED",
            )
        if (
            slots.specialty is not None
            or slots.doctor is not None
            or preference_fields
            or "I-01" in intents
        ):
            changes = {field: getattr(slots, field) for field in preference_fields}
            if slots.doctor is not None:
                doctors = [
                    d
                    for d in await self.names.speaking()
                    if d.full_name.split()[0].casefold() == slots.doctor.casefold()
                ]
                if not doctors:
                    raise BookingError("DOCTOR_UNAVAILABLE")
                changes["doctor_ids"] = tuple(d.id for d in doctors)
                specialties = {d.specialty for d in doctors}
                if len(specialties) != 1:
                    return state, reply(
                        state,
                        turn.turn_id,
                        "Уточните специальность врача.",
                        code="DOCTOR_AMBIGUOUS",
                    )
                if slots.specialty is None:
                    state = state.model_copy(update={"specialty": next(iter(specialties))})
            if slots.specialty is not None:
                if state.specialty is None or profile_key(state.specialty) != profile_key(
                    slots.specialty
                ):
                    state = state.model_copy(update={"preferences": SlotPreferences()})
                state = state.model_copy(
                    update={
                        "specialty": slots.specialty,
                        "referral_mentioned": state.referral_mentioned
                        or "направлен" in turn.text.casefold(),
                    }
                )
            state = state.model_copy(
                update={
                    "preferences": state.preferences.model_copy(update=changes),
                    "misunderstandings": 0,
                }
            )
            if state.client_id is None:
                state = state.model_copy(update={"phase": Phase.IDENTIFYING})
                return state, reply(
                    state,
                    turn.turn_id,
                    self.presenter.renderer.render(
                        "ident.ask_child" if state.guardian_call else "ident.ask_patient",
                        self.presenter._today(),
                    ).text,
                )
            if state.specialty:
                return await self.offer(state, turn)
        if "choice" in intents and state.phase == Phase.OFFERED:
            index = slots.choice_index
            if index is None or not 1 <= index <= len(state.options):
                raise BookingError("INVALID_CHOICE")
            doctors = {d.id: d for d in await self.names.speaking()}
            action = await self.booking.propose(
                state.call_id, state.client_id, state.options[index - 1]
            )
            state = state.model_copy(
                update={
                    "phase": Phase.HELD,
                    "proposal": Proposal(
                        action_id=action.action_id,
                        revision=action.revision,
                        slot=action.slot,
                        expires_at=action.expires_at,
                        doctor_name=doctors[action.slot.doctor_id].full_name,
                    ),
                    "misunderstandings": 0,
                }
            )
            return state, await self.confirmation_phrase(state, turn)
        if "yes" in intents and state.phase == Phase.HELD and state.proposal is not None:
            if not decision.final or not await self.store.authorize(
                state.call_id, state.version, state.input_epoch, turn.turn_id
            ):
                raise BookingError("CONFIRMATION_INTERRUPTED")
            action = await self.booking.confirm(
                Approval(
                    call_id=state.call_id,
                    action_id=state.proposal.action_id,
                    revision=state.proposal.revision,
                    turn_id=turn.turn_id,
                    is_final=True,
                    permitted=True,
                    guard_version=state.version,
                    input_epoch=state.input_epoch,
                )
            )
            state = state.model_copy(update={"authorization": None})
            if action.state == ActionState.BOOKED:
                confirmed = self.booked_phrase(action, state.proposal.doctor_name)
                referral_done = self._referral_done(state)
                state = state.model_copy(
                    update={
                        "phase": Phase.BOOKED,
                        "visit_id": action.visit_id,
                        "options": (),
                        "proposal": None,
                    }
                )
                return state, reply(
                    state,
                    turn.turn_id,
                    confirmed,
                    *(referral_done or (FOLLOW_UP_QUESTION,)),
                    code="BOOKED_GP" if state.referral_fallback else "BOOKED",
                )
            if action.state in {ActionState.COMMITTING, ActionState.RECONCILING}:
                state = state.model_copy(update={"phase": Phase.RECONCILING})
                return state, reply(
                    state,
                    turn.turn_id,
                    "Результат уточняется. Повторную запись не отправляю.",
                    code="RECONCILING",
                )
            state = state.model_copy(
                update={"phase": Phase.IDENTIFIED, "proposal": None, "options": ()}
            )
            return state, reply(
                state,
                turn.turn_id,
                "МИС отклонила запись. Запросите новые варианты.",
                code="BOOKING_REJECTED",
            )
        if "no" in intents:
            if state.phase == Phase.BOOKED:
                state = await self.store.end(state.call_id)
                return state, reply(state, turn.turn_id, "Всего доброго.", code="CALL_ENDED")
            await self.booking.store.release_call_holds(state.call_id)
            state = state.model_copy(
                update={
                    "phase": Phase.IDENTIFIED if state.client_id else Phase.READY,
                    "proposal": None,
                    "options": (),
                    "authorization": None,
                }
            )
            return state, reply(state, turn.turn_id, "Бронь снята. Уточните удобное время.")
        if "repeat" in intents:
            if state.phase == Phase.HELD:
                return state, await self.confirmation_phrase(state, turn)
            if state.client_id and state.specialty:
                return await self.offer(state, turn)
        count = state.misunderstandings + 1
        state = state.model_copy(update={"misunderstandings": count, "authorization": None})
        if count >= 3:
            return await self.transfer(state, turn, "R-05")
        return state, reply(
            state,
            turn.turn_id,
            "Не поняла. Повторите, пожалуйста."
            if count == 1
            else "Не удалось понять. Могу подготовить запрос регистратору.",
            code="NOT_UNDERSTOOD",
        )

    def room_phrase(self, slot) -> str:
        room = self.rooms.room(slot.doctor_id, slot.scheduled_at) if self.rooms else None
        return f", кабинет {room}" if room else ""

    def booked_phrase(self, action, doctor_name: str | None) -> str:
        room = (
            self.rooms.room(action.slot.doctor_id, action.slot.scheduled_at) if self.rooms else None
        )
        return self.presenter.completed(action.slot, doctor_name, room)

    @staticmethod
    def _referral_done(state: DialogueState) -> tuple[str, ...] | None:
        specialty = next(
            (
                item
                for key, item in load_specialties().items()
                if profile_key(key) == profile_key(state.specialty or "")
            ),
            None,
        )
        return (
            (load_phrasebook().get("referral.done").text,)
            if specialty and specialty.referral
            else None
        )

    async def confirmation_phrase(self, state: DialogueState, turn: Turn) -> Reply:
        slot = state.proposal.slot
        doctor_name = state.proposal.doctor_name
        if doctor_name is None:
            doctors = {d.id: d for d in await self.names.speaking()}
            doctor_name = doctors[slot.doctor_id].full_name
        return reply(
            state,
            turn.turn_id,
            *self.presenter.confirmation(
                slot,
                doctor_name,
                self.rooms.room(slot.doctor_id, slot.scheduled_at) if self.rooms else None,
            ),
            code="CONFIRMATION_REQUIRED",
        )
