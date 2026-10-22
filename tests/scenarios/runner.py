"""Выполняем YAML через HTTP API; ожидания не подставляются в NLU или ответы."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from types import SimpleNamespace

from app.clock import CLINIC_TIMEZONE
from app.dialogue.models import Reply
from app.handoffs.models import CallbackState, TransferState
from app.mis.models import VisitStatus
from tests.scenarios.runtime import TOKEN, ScenarioRuntime, moment

PHASES = {
    "ident.ask": {"identifying"},
    "ident.ask_name": {"identifying"},
    "ident.ask_birth_date": {"identifying"},
    "ident.ask_patient": {"identifying"},
    "booking.offer_first": {"offered"},
    "booking.offer_period": {"offered"},
    "booking.offer_nearest": {"offered"},
    "booking.confirm": {"held"},
    "booking.done": {"booked"},
    "booking.done_child": {"booked"},
    "cancel.which": {"cancel_choosing"},
    "cancel.offer_reschedule": {"cancel_offer"},
    "cancel.confirm": {"cancel_confirm"},
    "cancel.done": {"cancelled"},
    "reschedule.found_one": {"move_source"},
    "reschedule.offer": {"move_offered"},
    "reschedule.confirm": {"move_confirm"},
    "reschedule.done": {"rescheduled"},
    "my_visits.one": {"visits_shown"},
    "my_visits.third_party": {"verifying_owner"},
    "closing.goodbye": {"ended"},
    "closing.goodbye_short": {"ended"},
    "waitlist.offer": {"waitlist_consent"},
    "waitlist.ask_when": {"waitlist_preferences"},
    "waitlist.done": {"identified"},
}
CODE_EXPECTATIONS = {
    "waitlist.offer": {"WAITLIST_CONSENT_REQUIRED"},
    "waitlist.ask_when": {"WAITLIST_PREFERENCES_REQUIRED"},
    "waitlist.done": {"WAITLISTED"},
    "medical.boundary": {"MEDICAL_BOUNDARY"},
    "robot.answer": {"ROBOT_DISCLOSED"},
    "recording.off": {"RECORDING_OPT_OUT"},
    "emergency.call_103": {"EMERGENCY"},
    "emergency.self_harm": {"EMERGENCY"},
    "reask.first": {"NOT_UNDERSTOOD"},
    "remind.clarify_maybe": {"REMINDER_CLARIFICATION"},
    "ident.not_found": {"IDENTIFICATION_REQUIRED"},
    "emergency.insist": {"EMERGENCY"},
    "emergency.ambulance_called": {"EMERGENCY"},
    "ident.under_15": {"MINOR_BOOKING_REFUSED"},
}
GAPS = {
    "labs.": "labs_status_source_T09",
    "attach.": "faq_T09",
    "silence.": "voice_event_channel_T03",
    "complaint.callback": "callback_T08",
    "transfer.outside_hours": "callback_T08",
    "wait.schedule": "intermediate_wait_prompt_T03_T05",
}

TEXT_REQUIREMENTS = {
    "ident.ask_policy": r"полис",
    "reask.offer_operator": r"оператор|сотрудник|регистратор",
}

# Проверяем и происхождение, и содержание; одного INFO_GIVEN недостаточно.
INFORMATION_EXPECTATIONS = {
    "schedule.specialty": (
        "doctor_schedule_unavailable",
        (r"график|расписание", r"в справочнике нет|не указан|не могу подтвердить", r"регистратор"),
    ),
    "transfer.ministry": (
        "medicine_benefits",
        (r"колл-центр Минздрава", r"сто двадцать два"),
    ),
    "transfer.insurer": (
        "insurance_policy",
        (r"страховая компания", r"телефон указан на полисе"),
    ),
    "attach.how": (
        "attachment",
        (r"Госуслуг", r"окне номер один", r"паспорт", r"полис", r"СНИЛС", r"трех рабочих дней"),
    ),
    "attach.before": (
        "before_attachment",
        (r"Неотложную и экстренную помощь", r"после прикрепления", r"платно"),
    ),
}

LABORATORY_EXPECTATIONS = {
    "labs.ask_identity": (
        {"IDENTIFICATION_REQUIRED"},
        ("lab_status",),
        (r"фамилию, имя и отчество пациента",),
    ),
    "labs.ask_type": (
        {"INFORMATION_REQUIRED", "INFO_UNAVAILABLE"},
        ("lab_terms", "lab_status"),
        (r"какое именно исследование",),
    ),
    "labs.general_terms": (
        {"INFO_GIVEN"},
        ("lab:",),
        (r"обычный срок", r"рабочи", r"не проверяю|не подтверждение"),
    ),
    "labs.personal_status": (
        {"INFO_GIVEN"},
        ("lab_status",),
        (r"от одного до четырнадцати рабочих дней", r"проверить не могу"),
    ),
    "labs.how_to_get": (
        {"INFO_GIVEN", "MEDICAL_BOUNDARY"},
        ("lab_collection", "lab_values"),
        (
            r"Госуслуг",
            r"окне номер три",
            r"лечащего врача",
            r"двенадцати до девятнадцати",
            r"по будням",
        ),
    ),
    "labs.values_refused": (
        {"MEDICAL_BOUNDARY"},
        ("lab_values",),
        (r"значений результатов я не вижу", r"не могу их оценивать", r"Записать вас к терапевту"),
    ),
    "labs.no_values": (
        {"MEDICAL_BOUNDARY"},
        ("lab_values",),
        (r"значений результатов я не вижу", r"не могу их оценивать", r"Записать вас к терапевту"),
    ),
}


@dataclass
class Finding:
    code: str
    detail: str
    kind: str = "fail"
    step: int | None = None


@dataclass
class DialogResult:
    scenario_id: str
    label: str
    blocking: bool
    status: str = "pass"
    steps: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    requests: dict = field(
        default_factory=lambda: {"total": 0, "by_method_resource": {}, "requests": []}
    )
    mutations: dict = field(
        default_factory=lambda: {
            "visits_created": 0,
            "visits_patched": 0,
            "clients_patched": 0,
            "escalations_created": 0,
        }
    )
    evidence: dict = field(default_factory=dict)
    elapsed_ms: float = 0
    simulated_seconds: float = 0
    budget_profile: str = "functional_unlimited_emulator"

    def add(self, code, detail, kind="fail", step=None):
        finding = Finding(code, detail, kind, step)
        if finding not in self.findings:
            self.findings.append(finding)

    def finish(self):
        kinds = {f.kind for f in self.findings}
        self.status = (
            "fail"
            if kinds & {"fail", "critical", "error"}
            else ("unsupported" if "unsupported" in kinds else "pass")
        )
        return self

    def serializable(self):
        return asdict(self)


def templates(step):
    value = step.get("say", [])
    return [value] if isinstance(value, str) else value


async def verify_step(result, number, step, response, runtime):
    text = " ".join(response.utterances).casefold().replace("ё", "е")
    public_data = response.model_dump_json().casefold().replace("ё", "е")
    for forbidden in step.get("forbid", ()):
        if forbidden.casefold().replace("ё", "е") in public_data:
            result.add(
                "forbidden_disclosure",
                "В ответ попал запрещённый фрагмент фикстуры",
                "critical",
                number,
            )
    if "my_visits.third_party" in templates(step) and any(
        (
            response.options,
            response.visits,
            response.proposal,
            response.visit_id,
            response.cancel_proposal,
            response.move_proposal,
        )
    ):
        result.add(
            "structured_disclosure",
            "Раскрыты данные визита до проверки владельца",
            "critical",
            number,
        )
    if "transfer" in step:
        wanted = step["transfer"]
        if response.transfer is None or any(
            getattr(response.transfer, key) != value for key, value in wanted.items()
        ):
            result.add("transfer", f"Ожидается {wanted['route']}/{wanted['priority']}", step=number)
    if "options" in step:
        expected = [
            moment(item if isinstance(item, str) else item["at"]) for item in step["options"]
        ]
        actual = [
            slot.scheduled_at for slot in (response.visits if response.visits else response.options)
        ]
        if actual != expected:
            result.add("options", "Предложенные даты/время не совпадают с YAML", step=number)
    if step.get("slot_weekdays") and any(
        slot.scheduled_at.isoweekday() not in step["slot_weekdays"] for slot in response.options
    ):
        result.add("slot_weekdays", "Предложен запрещённый день недели", step=number)
    for name in templates(step):
        if name == "ident.ask_child":
            if not re.search(r"реб[её]нка", text, re.IGNORECASE):
                result.add("guardian_T10", "Не запрошены данные ребёнка", step=number)
            continue
        if name == "ident.relation":
            state = await runtime.dialogue_store.inspect(runtime.call_id)
            expected_identity = step.get("parse", {}).get("slots", {})
            patient = runtime.emulator.clients[TOKEN].get(state.client_id)
            child_matches = patient is not None and all(
                str(getattr(patient, field, None)) == str(value)
                for field, value in expected_identity.items()
                if field in {"last_name", "first_name", "middle_name", "birth_date"}
            )
            if (
                not re.search(r"кем вы приходитесь", text, re.IGNORECASE)
                or not child_matches
                or not state.guardian_call
            ):
                result.add(
                    "guardian_T10",
                    "Не подтверждены идентификация ребёнка и вопрос о родстве звонящего",
                    step=number,
                )
            continue
        if name == "booking.offer_nearest" and result.scenario_id == "M-12":
            state = await runtime.dialogue_store.inspect(runtime.call_id)
            if not state.guardian_call or state.guardian_relation != "мама":
                result.add("guardian_T10", "Не сохранена роль звонящего как матери", step=number)
            continue
        if name == "callback.offer":
            if response.code != "CALLBACK_OFFERED":
                result.add(
                    "callback_offer_T08",
                    "Не создано предложение обратного звонка через HandoffService",
                    step=number,
                )
            continue
        if name == "callback.created":
            if response.code != "CALLBACK_QUEUED" or response.phase != "ended":
                result.add(
                    "callback_result_T08",
                    "Ответ не подтверждает сохранённую задачу обратного звонка",
                    step=number,
                )
            continue
        if name == "callback.mis_down":
            request = await runtime.handoff_store.request_for_call(runtime.call_id)
            tasks = await runtime.handoff_store.list_callback_tasks(limit=10000, offset=0)
            visit_posts = [
                item
                for item in runtime.emulator.requests
                if item.method == "POST" and item.resource == "visits"
            ]
            if (
                response.code != "RECONCILING"
                or response.phase != "reconciling"
                or runtime.emulator.write_count != 0
                or len(visit_posts) != 1
                or request is not None
                or tasks
            ):
                result.add(
                    "callback_mis_down_T08",
                    "Неопределённый POST должен остаться на сверке без повтора и ложной callback-задачи",
                    step=number,
                )
            continue
        callback_steps = {
            "callback.contact": (r"CALLBACK_STEP", r"номер телефона"),
            "callback.time": (r"CALLBACK_STEP", r"удобное время|ближайшее время"),
            "callback.confirm": (r"CALLBACK_STEP", r"сохранить|подтвердите"),
        }
        if name in callback_steps:
            code, text_pattern = callback_steps[name]
            if response.code != code or re.search(text_pattern, text, re.IGNORECASE) is None:
                result.add(
                    "callback_step_T08",
                    "Не подтверждён ожидаемый шаг оформления callback",
                    step=number,
                )
            continue
        if name.startswith("referral."):
            valid = True
            if name == "referral.missing":
                valid = (
                    response.code == "REFERRAL_REQUIRED"
                    and response.expect == "yes_no"
                    and re.search(r"направлен|участков", text, re.IGNORECASE) is not None
                )
            elif name == "referral.found":
                state = await runtime.dialogue_store.inspect(runtime.call_id)
                client_id = state.client_id
                valid = (
                    bool(response.options)
                    and client_id is not None
                    and all(
                        runtime.schedule.access_state(client_id, slot) == "allowed"
                        for slot in response.options
                    )
                    and re.search(r"направлен", text, re.IGNORECASE) is not None
                )
            elif name == "referral.done":
                valid = (
                    response.code == "BOOKED"
                    and response.phase == "booked"
                    and re.search(r"паспорт", text, re.IGNORECASE) is not None
                    and re.search(r"полис", text, re.IGNORECASE) is not None
                )
            elif name == "referral.paper":
                valid = (
                    response.transfer is not None
                    and response.transfer.route == "R-08"
                    and response.transfer.priority == "P3"
                    and re.search(r"бумажн.*направлен|направлен.*регистратор", text, re.IGNORECASE)
                    is not None
                )
            if not valid:
                result.add(
                    "referral:" + name,
                    "Не подтверждён маршрут или результат работы с направлением",
                    step=number,
                )
            continue
        if name == "paid.price":
            configured = next(
                (
                    item
                    for item in runtime.dialog.setup.get("prices", ())
                    if item["service"].casefold() in text
                ),
                None,
            )
            expected_text = (
                runtime.service.conversation.presenter.renderer.render(
                    "paid.price",
                    runtime.service.booking.clock.now().date(),
                    service=configured["service"],
                    price=configured["price"],
                ).text
                if configured is not None
                else None
            )
            if (
                response.code != "INFO_GIVEN"
                or expected_text is None
                or expected_text.casefold() not in text
                or response.phase != "ready"
            ):
                result.add(
                    "paid_price_T09",
                    "Не подтверждён ответ по цене из настроенного справочника",
                    step=number,
                )
            continue
        if name.startswith("home_visit."):
            expected = {
                "home_visit.late": ("HOME_VISIT_CONTINUE", r"завтра|среду"),
                "home_visit.safety": ("HOME_VISIT_IDENTITY", r"сто три|сто 103"),
                "home_visit.ask_address": ("HOME_VISIT_ADDRESS", r"адрес"),
                "home_visit.ask_reason": ("HOME_VISIT_QUESTION", r"что случилось"),
                "home_visit.ask_can_come": ("HOME_VISIT_QUESTION", r"сами прийти"),
                "home_visit.pending": ("HOME_VISIT_PENDING", r"просьбу для проверки регистратурой"),
                "home_visit.done": ("HOME_VISIT_PENDING", r"вызов врача пока не подтвержден"),
            }
            code, phrase = expected[name]
            if response.code != code or re.search(phrase, text, re.IGNORECASE) is None:
                result.add(
                    "home_visit_T09", "Не подтверждён ответ по вызову врача на дом", step=number
                )
            if name == "home_visit.pending":
                request = await runtime.handoff_store.request_for_call(runtime.call_id)
                if request is None or request.home_visit_state != "pending_confirmation":
                    result.add(
                        "home_visit_T09",
                        "Заявка не сохранена с ожидающим подтверждением",
                        step=number,
                    )
            continue
        gap = next((value for prefix, value in GAPS.items() if name.startswith(prefix)), None)
        if name == "remind.details":
            if response.code != "REMINDER_DETAILS" or response.phase != "remind_details":
                result.add(
                    "reminder_details", "Адресат не подтвердил переход к деталям", step=number
                )
            continue
        if name == "remind.confirmed":
            if (
                response.reminder_result is None
                or response.reminder_result.outcome != "CONFIRMED"
                or response.reminder_result.mis_status != "applied"
            ):
                result.add("reminder_confirm", "Отметка подтверждения не применена", step=number)
            continue
        if name == "remind.wrong_person":
            if (
                response.reminder_result is None
                or response.reminder_result.outcome != "WRONG_PERSON"
            ):
                result.add(
                    "reminder_wrong_person", "Не зафиксирован неправильный адресат", step=number
                )
            continue
        if name == "remind.voicemail":
            if response.reminder_result is None or response.reminder_result.outcome != "VOICEMAIL":
                result.add(
                    "reminder_voicemail",
                    "Событие не завершилось исходом автоответчика",
                    step=number,
                )
            continue
        if name == "remind.maybe":
            if response.reminder_result is None or response.reminder_result.outcome != "MAYBE":
                result.add("reminder_maybe", "Не зафиксирован неопределённый ответ", step=number)
            continue
        if name == "remind.opt_out":
            if response.reminder_result is None or response.reminder_result.outcome != "OPT_OUT":
                result.add("reminder_opt_out", "Не зафиксирован отказ от обзвона", step=number)
            continue
        if name in {"remind.offer_reschedule", "remind.done_moved"}:
            if name == "remind.offer_reschedule" and response.code not in {
                "REMINDER_DECLINED",
                "MOVE_OPTIONS",
            }:
                result.add("reminder_move", "Не предложен безопасный перенос", step=number)
            elif name == "remind.done_moved" and response.reminder_result is not None:
                if response.reminder_result.outcome != "RESCHEDULED":
                    result.add("reminder_move", "Перенос завершился другим исходом", step=number)
            continue
        if name in LABORATORY_EXPECTATIONS:
            codes, topics, required = LABORATORY_EXPECTATIONS[name]
            if (
                response.code not in codes
                or response.information is None
                or not response.information.topic.startswith(topics)
                or not response.information.sections
                or not all(re.search(pattern, text, re.IGNORECASE) for pattern in required)
            ):
                result.add(
                    "information:" + name,
                    "Не подтверждена требуемая лабораторная справка",
                    step=number,
                )
            continue
        if name == "labs.not_patient":
            if (
                response.code != "INFO_UNAVAILABLE"
                or response.information is None
                or response.information.topic != "lab_collection"
                or not re.search(r"только самому пациенту", text, re.IGNORECASE)
            ):
                result.add(
                    "lab_privacy",
                    "Не отказано в выдаче персонального статуса анализов родственнику",
                    "critical",
                    number,
                )
            continue
        if name in INFORMATION_EXPECTATIONS:
            topic, required = INFORMATION_EXPECTATIONS[name]
            if (
                response.code != "INFO_GIVEN"
                or response.information is None
                or response.information.topic != topic
                or not response.information.sections
                or not all(re.search(pattern, text, re.IGNORECASE) for pattern in required)
            ):
                result.add(
                    "information:" + name, "Не подтверждена требуемая общая справка", step=number
                )
            continue
        elif gap:
            result.add(gap, "Не подключено требование: " + name, "unsupported", number)
            continue
        if name in PHASES:
            if response.phase not in PHASES[name]:
                result.add("phase:" + name, f"Получено состояние {response.phase}", step=number)
            if name.startswith("booking.offer") and response.code in {
                "WAIT_MIS",
                "BOOKING_REJECTED",
                "MIS_UNAVAILABLE",
                "NO_SLOTS",
            }:
                result.add(
                    "offer_unavailable", f"Вместо вариантов получен {response.code}", step=number
                )
        elif name in TEXT_REQUIREMENTS:
            if re.search(TEXT_REQUIREMENTS[name], text) is None:
                result.add("text:" + name, "Не прозвучало обязательное уточнение", step=number)
        elif name in CODE_EXPECTATIONS:
            if response.code not in CODE_EXPECTATIONS[name]:
                result.add("code:" + name, f"Получен код {response.code}", step=number)
        elif name.startswith("transfer.") or name == "complaint.ack":
            if response.transfer is None:
                result.add("handoff:" + name, "В ответе отсутствует запрос перевода", step=number)
        elif name not in {"ask.how_can_help", "closing.anything_else"}:
            result.add(
                "unmapped:" + name,
                "Для шаблона ещё не задана проверка поведения",
                "unsupported",
                number,
            )
    if len(response.options) > 3:
        result.add("too_many_options", "Больше трёх вариантов", "critical", number)


async def verify_reminder_result(result, runtime, reminder_result):
    """Проверить доменный исход T11, локальный контакт и фактическую запись в эмуляторе."""
    from app.reminders.marks import confirmation_line

    outcome = reminder_result.outcome
    attempt = await runtime.reminder_store.attempt(runtime.reminder_attempt_id)
    reminder_job = await runtime.reminder_store.job(attempt.job_id)
    sms_events = await runtime.reminder_store.outbox_events(reminder_job.id)
    client_id = runtime.client_ids[runtime.dialog.setup["call"]["client"]]
    contact = await runtime.reminder_store.contact(client_id)
    if attempt.result != outcome:
        result.add("reminder_attempt", "Исполнитель не сохранил фактический исход звонка")
    if not reminder_job.sms_pending and outcome != "OPT_OUT":
        result.add("reminder_sms_pending", "Для визита не создано SMS-напоминание")
    expected_sms_event = reminder_job.sms_pending
    if (expected_sms_event and len(sms_events) != 1) or (not expected_sms_event and sms_events):
        result.add(
            "reminder_sms_event", "Исход напоминания не создал ровно одно событие SMS в outbox"
        )
    if sms_events and (
        sms_events[0].payload.kind != "reminder_sms"
        or sms_events[0].payload.client_id != reminder_job.client_id
        or sms_events[0].state
        != (
            "blocked"
            if contact is not None and contact.notification_opt_out_at is not None
            else "pending"
        )
    ):
        result.add("reminder_sms_event", "Событие SMS имеет неверного адресата или состояние")
    client = runtime.emulator.clients[TOKEN][client_id]
    if outcome == "CONFIRMED":
        mark = await runtime.reminder_store.mark(f"confirm:{runtime.reminder_attempt_id}")
        expected = confirmation_line(runtime.initial_visit.id, runtime.initial_visit.scheduled_at)
        if (
            mark is None
            or mark.state != "applied"
            or client.comment != "Прежняя заметка регистратора\n" + expected
            or runtime.emulator.client_patch_count != 1
        ):
            result.add("reminder_mark", "PATCH или журнал отметки не подтверждены фактически")
    elif outcome == "OPT_OUT":
        if contact is None or contact.opt_out_at is None:
            result.add("reminder_contact_opt_out", "Локальный контакт не исключён из обзвона")
        if runtime.emulator.client_patch_count:
            result.add(
                "reminder_identity_write", "Карточка изменена до проверки адресата", "critical"
            )
    elif outcome in {"WRONG_PERSON", "VOICEMAIL", "MAYBE"}:
        if (
            runtime.emulator.client_patch_count
            or runtime.emulator.patch_count
            or runtime.emulator.write_count
        ):
            result.add(
                "reminder_unexpected_write",
                "МИС изменилась без подтверждённого действия",
                "critical",
            )
    mark = await runtime.reminder_store.mark(f"confirm:{runtime.reminder_attempt_id}")
    result.evidence["outbound"] = {
        "path": "ReminderExecutor.tick → ReminderActionAdapter → DialogueService",
        "substitutions": [
            "телефонный originate/answer: ScenarioDialer",
            "HTTP МИС: ScenarioEmulator",
            "задания, попытки и отметки: MemoryReminderStore",
        ],
        "attempt_state": attempt.state,
        "outcome": outcome,
        "sms_pending": reminder_job.sms_pending,
        "sms_event_state": sms_events[0].state if sms_events else None,
        "mis_status": reminder_result.mis_status,
        "mark_state": mark.state if mark is not None else None,
        "client_comment_patches": runtime.emulator.client_patch_count,
        "preserved_existing_comment": (
            client.comment is not None and client.comment.startswith("Прежняя заметка регистратора")
        ),
        "contact_opt_out": contact is not None and contact.opt_out_at is not None,
        "notification_opt_out": (
            contact is not None and contact.notification_opt_out_at is not None
        ),
        "voicemail_event_port": outcome == "VOICEMAIL",
    }


async def verify_handoff_card(result, runtime, response, expectation, number):
    request = await runtime.handoff_store.request_for_call(runtime.call_id)
    if request is None:
        result.add("operator_card_T08", "HandoffService не сохранил карточку перевода", step=number)
        return
    expected_route = expectation.get("route", expectation.get("rule"))
    expected_priority = expectation.get("priority")
    if expected_route and request.route != expected_route:
        result.add("operator_card_T08", "Маршрут карточки не совпадает с YAML", step=number)
    if expected_priority and request.priority != expected_priority:
        result.add("operator_card_T08", "Приоритет карточки не совпадает с YAML", step=number)
    if (
        response is not None
        and response.transfer is not None
        and (
            request.route != response.transfer.route
            or request.priority != response.transfer.priority
        )
    ):
        result.add("operator_card_T08", "Карточка не совпадает с маршрутом ответа", step=number)
    state = await runtime.dialogue_store.inspect(runtime.call_id)
    expected_identity = "verified" if state.client_id is not None else "unknown"
    if request.identity_status != expected_identity:
        result.add(
            "operator_card_T08",
            "Статус идентификации карточки не совпадает с диалогом",
            step=number,
        )
    if not request.summary.strip():
        result.add("operator_card_T08", "В карточке отсутствует краткое содержание", step=number)
    if request.identity_status == "verified":
        if request.client_id != state.client_id:
            result.add("operator_card_T08", "Карточка содержит неверный client_id", step=number)
    elif request.client_id is not None:
        result.add(
            "operator_card_T08", "В карточке без идентификации указан client_id", step=number
        )
    expected_representative = expectation.get("caller_is_representative")
    if expected_representative is not None:
        if request.caller_is_representative is not expected_representative:
            result.add(
                "guardian_T10",
                "В карточке перевода неверно отмечен звонок представителя",
                step=number,
            )
        if expected_representative and (
            not state.guardian_call or state.guardian_relation != "мама"
        ):
            result.add(
                "guardian_T10",
                "Не идентифицирован пациент или не сохранено родство звонящего",
                step=number,
            )
    tasks = await runtime.handoff_store.list_callback_tasks(limit=10000, offset=0)
    request_tasks = [task for task in tasks if task.request_id == request.request_id]
    if request.priority == "P1" and (request.callback_state is not None or request_tasks):
        result.add("callback_T08", "Для P1 недопустима задача обратного звонка", step=number)
    result.evidence["handoff_card"] = {
        "route": request.route,
        "priority": request.priority,
        "identity_status": request.identity_status,
        "summary_present": bool(request.summary.strip()),
        "client_id_present": request.client_id is not None,
        "caller_is_representative": request.caller_is_representative,
        "transfer_state": request.transfer_state.value,
    }


def _expected_callback_deadline(now: datetime, priority: str) -> datetime:
    """Independent scenario expectation for §7.6, including 2026 non-working days."""
    now = now.astimezone(CLINIC_TIMEZONE)
    if priority not in {"P2", "P3"}:
        raise ValueError("callback is only valid for P2/P3")
    holidays = {datetime(2026, 1, day).date() for day in range(1, 12)} | {
        datetime(2026, month, day).date()
        for month, days in {
            2: (23,),
            3: (9,),
            5: (1, 9, 11),
            6: (12,),
            11: (4,),
            12: (31,),
        }.items()
        for day in days
    }

    def hours(day):
        if day.weekday() == 6 or day in holidays:
            return None
        return (8, 30, 15) if day.weekday() == 5 else (7, 0, 20)

    opening = hours(now.date())
    minutes = 15 if priority == "P2" else 30
    candidate = now + timedelta(minutes=minutes)
    if (
        opening is not None
        and (opening[0], opening[1]) <= (now.hour, now.minute)
        and now.time()
        < datetime(now.year, now.month, now.day, opening[2], tzinfo=now.tzinfo).time()
        and candidate.date() == now.date()
        and candidate.hour * 60 + candidate.minute <= opening[2] * 60
    ):
        return candidate
    day = now.date() + timedelta(days=1)
    while hours(day) is None:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=CLINIC_TIMEZONE)


async def verify_callback_task(result, runtime, expectation, responses):
    request = await runtime.handoff_store.request_for_call(runtime.call_id)
    task = (
        await runtime.handoff_store.get_callback_task_by_request(request.request_id)
        if request is not None
        else None
    )
    if request is None or task is None:
        result.add("callback_task_T08", "HandoffService не сохранил задачу обратного звонка")
        return
    tasks = await runtime.handoff_store.list_callback_tasks(limit=10000, offset=0)
    request_tasks = [item for item in tasks if item.request_id == request.request_id]
    if len(request_tasks) != 1:
        result.add(
            "callback_task_T08", f"Ожидалась ровно одна задача, сохранено {len(request_tasks)}"
        )
        return
    expected_state = expectation.get("state", "queued")
    if task.state.value != expected_state or request.callback_state != CallbackState(
        expected_state
    ):
        result.add("callback_task_T08", "Состояние задачи не совпадает с YAML")
    if not task.contact_masked or not task.contact_ciphertext or task.priority != request.priority:
        result.add("callback_task_T08", "В задаче не подтверждены номер и приоритет")
    expected_deadline = _expected_callback_deadline(runtime.clock.now(), request.priority)
    if task.deadline != expected_deadline or request.callback_deadline != expected_deadline:
        result.add("callback_deadline_T08", "Сохранённый срок не совпадает с §7.6")
    before = expectation.get("before")
    exact = expectation.get("deadline")
    if before and task.deadline > moment(before):
        result.add("callback_deadline_T08", "Срок позже предела из YAML")
    if exact and task.deadline != moment(exact):
        result.add("callback_deadline_T08", "Срок не совпадает с YAML")
    phrase = f"{task.deadline.day:02}.{task.deadline.month:02} в {task.deadline:%H:%M}"
    spoken = " ".join(" ".join(reply.utterances) for reply in responses)
    if phrase not in spoken:
        result.add("callback_deadline_T08", "Ответ не называет срок обратного звонка")
    result.evidence["callback_task"] = {
        "state": task.state.value,
        "priority": task.priority,
        "contact_masked": task.contact_masked,
        "deadline": task.deadline.isoformat(),
        "deadline_spoken": phrase in spoken,
        "task_count": len(request_tasks),
    }


async def record_fake_transfer(
    result, runtime, *, outcome, number, start_callback, responses, turn_id=None
):
    request = await runtime.handoff_store.request_for_call(runtime.call_id)
    if request is None:
        result.add("handoff_completion_T08_T04", "Нет карточки для исхода перевода", step=number)
        return None
    if outcome not in {"no_answer", "busy", "connected", "failed"}:
        result.add("handoff_completion_T08_T04", "Неизвестный fake outcome из YAML", step=number)
        return None
    stored_outcome = "failed" if outcome in {"busy", "failed"} else outcome
    attempt_id = f"scenario-{number}"
    state = await runtime.dialogue_store.inspect(runtime.call_id)
    event = SimpleNamespace(
        attempt_id=attempt_id,
        call_id=runtime.call_id,
        route=request.route,
        priority=request.priority,
    )
    await runtime.handoffs.transfer_started(event, state)
    await runtime.handoffs.transfer_result(
        SimpleNamespace(
            **vars(event),
            outcome=stored_outcome,
            reason="busy" if outcome == "busy" else "scenario",
        )
    )
    saved = await runtime.handoff_store.request_for_call(runtime.call_id)
    if saved is None or saved.transfer_state != TransferState(stored_outcome):
        result.add(
            "handoff_completion_T08_T04", "HandoffService не сохранил исход перевода", step=number
        )
        return None
    transfer_replies = [reply for reply in responses if reply.transfer is not None]
    if not transfer_replies or any(
        reply.transfer.route != saved.route or reply.transfer.priority != saved.priority
        for reply in transfer_replies
    ):
        result.add(
            "handoff_completion_T08_T04",
            "Диалоговый ответ не совпадает с фактически сохранённой карточкой",
            step=number,
        )
    result.evidence["handoff_completion"] = {
        "evidence": "fake_transfer",
        "outcome": outcome,
        "stored_state": saved.transfer_state.value,
        "reason": saved.transfer_reason,
    }
    if outcome == "connected" or saved.priority == "P1":
        tasks = await runtime.handoff_store.list_callback_tasks(limit=10000, offset=0)
        if saved.callback_state is not None or any(t.request_id == saved.request_id for t in tasks):
            result.add("callback_T08", "Для перевода без callback создана задача", step=number)
        return None
    if not start_callback:
        result.add(
            "callback_offer_T08",
            "Для исхода без ответа не заданы шаги согласия, контакта и времени",
            "unsupported",
            number,
        )
        return None
    turn_id = turn_id if turn_id is not None else state.last_turn_id + 1
    callback_reply = await runtime.service.start_callback(runtime.call_id, turn_id, outcome)
    if callback_reply is None:
        result.add("callback_offer_T08", "HandoffService не предложил обратный звонок", step=number)
        return None
    return callback_reply


def verify_records(result, runtime, expectation):
    actual = runtime.snapshot()
    initial = runtime.initial_records
    if expectation == "unchanged":
        if actual != initial:
            result.add("stand_changed", "МИС изменилась в сценарии без изменений", "critical")
        return
    if not isinstance(expectation, dict):
        return
    if set(initial) - set(actual):
        result.add("visit_deleted", "Исходная запись пропала из стенда", "critical")
    expected_created = list(expectation.get("created", ()))
    expected_created += [dict(item, at=item["to"]) for item in expectation.get("rescheduled", ())]
    created = {key: value for key, value in actual.items() if key not in initial}
    if created and not any(key in expectation for key in ("created", "rescheduled", "at_most_one")):
        result.add("unexpected_create", "В сценарии появилась лишняя новая запись", "critical")
    if "created" in expectation or "rescheduled" in expectation:
        if len(created) != len(expected_created):
            result.add(
                "created_count", f"Ожидается {len(expected_created)}, получено {len(created)}"
            )
        for reference in expected_created:
            matches = [
                v
                for v in created.values()
                if runtime.matches_visit(v, reference) and v["status"] == VisitStatus.PLANNED
            ]
            if len(matches) != 1:
                result.add("created_visit", "Нет единственного ожидаемого нового визита")
    cancelled = list(expectation.get("cancelled", ()))
    cancelled += [dict(item, at=item["from"]) for item in expectation.get("rescheduled", ())]
    expected_changed = set()
    for reference in cancelled:
        matches = [key for key, v in initial.items() if runtime.matches_visit(v, reference)]
        if len(matches) != 1 or actual.get(matches[0], {}).get("status") != VisitStatus.CANCELLED:
            result.add("cancelled_visit", "Исходная запись не отменена ожидаемым образом")
        if len(matches) == 1 and matches[0] in actual:
            key = matches[0]
            if any(
                actual[key][field] != initial[key][field]
                for field in ("id", "client_id", "doctor", "scheduled_at", "created_at")
            ):
                result.add(
                    "cancelled_visit_fields", "При отмене изменены другие поля визита", "critical"
                )
        expected_changed.update(matches)
    for identifier, before in initial.items():
        if identifier not in expected_changed and actual.get(identifier) != before:
            result.add(
                "unrelated_visit_changed", "Изменена посторонняя исходная запись", "critical"
            )
    for reference in expectation.get("unchanged", ()):
        if not any(
            runtime.matches_visit(v, reference) and actual.get(key) == v
            for key, v in initial.items()
        ):
            result.add("expected_unchanged_visit", "Посторонняя запись не сохранена", "critical")
    if "at_most_one" in expectation:
        count = sum(runtime.matches_visit(v, expectation["at_most_one"]) for v in actual.values())
        if count > 1:
            result.add("duplicate_visit", "После отказа создано больше одного визита", "critical")


VOICE_REQUIREMENTS = {
    "event:silence": {"R-17"},
    "intermediate_wait_prompt_T03_T05": {"D-13"},
    "recording_engine_T13": {"D-15"},
}


def apply_voice_evidence(results, path: Path, source: dict) -> None:
    document = json.loads(path.read_text("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("source"), dict):
        raise ValueError("voice evidence must contain a source object")
    rows = document.get("results")
    if not isinstance(rows, list):
        raise ValueError("voice evidence must contain a results array")

    evidence_source = document["source"]
    evidence_commit = evidence_source.get("commit")
    evidence_hash = evidence_source.get("source_sha256")
    source_matches = (
        bool(evidence_commit)
        and evidence_commit == source.get("commit")
        and bool(evidence_hash)
        and evidence_hash == source.get("source_sha256")
    )

    for row in rows:
        if not isinstance(row, dict):
            continue
        requirement = row.get("requirement")
        scenario_id = row.get("scenario_id")
        if (
            requirement not in VOICE_REQUIREMENTS
            or scenario_id not in VOICE_REQUIREMENTS[requirement]
        ):
            continue
        target = next(
            (
                result
                for result in results
                if result.scenario_id == scenario_id and result.label == row.get("label")
            ),
            None,
        )
        if target is None:
            continue
        accepted = source_matches and row.get("verdict") == "ok"
        if requirement == "event:silence":
            accepted = accepted and row.get("result_code") == "DROPPED"
        target.evidence["voice_session"] = {
            "evidence": "voice_session" if accepted else "unsupported",
            "requirement": requirement,
            "source_commit": evidence_source.get("commit"),
            "source_sha256": evidence_hash,
            "verdict": row.get("verdict"),
            "detail": (
                "голосовое доказательство относится к другому исходному дереву"
                if not source_matches
                else row.get("detail", "голосовое доказательство не подтверждено")
            ),
        }
        if not accepted:
            target.finish()
            continue
        target.findings = [
            finding
            for finding in target.findings
            if not (finding.kind == "unsupported" and finding.code == requirement)
        ]
        if requirement == "event:silence" and row.get("result_code") == "DROPPED":
            target.findings = [
                finding
                for finding in target.findings
                if not (finding.kind == "fail" and finding.code == "result_code")
            ]
        target.finish()


async def verify_waitlist(result, runtime, expectation):
    store = runtime.service.conversation.waitlist_store
    entries = await store.by_call(runtime.call_id)
    if not entries:
        result.add("waitlist_T08", "Заявка отсутствует в хранилище")
        return
    if len(entries) != 1:
        result.add("waitlist_T08", "Для одного запроса создано несколько заявок")
        return
    entry = entries[0]
    if expectation is None:
        return
    if expectation.get("client"):
        client_id = runtime.client_ids[expectation["client"]]
        if entry.client_id != client_id:
            result.add("waitlist_T08", "В заявке указан другой пациент")
    if (
        expectation.get("specialty")
        and entry.specialty.casefold() != expectation["specialty"].casefold()
    ):
        result.add("waitlist_T08", "В заявке указана другая специальность")
    for key in ("time_from", "time_to"):
        if key in expectation:
            actual = getattr(entry.preferences, key)
            expected = datetime_time.fromisoformat(expectation[key])
            if actual != expected:
                result.add("waitlist_T08", f"В заявке не сохранено пожелание {key}")
    if "exclude_weekdays" in expectation and tuple(entry.preferences.exclude_weekdays) != tuple(
        expectation["exclude_weekdays"]
    ):
        result.add("waitlist_T08", "В заявке не сохранены исключённые дни недели")
    result.evidence["waitlist"] = {
        "request_id": entry.request_id,
        "client_reference_present": True,
        "specialty": entry.specialty,
        "preferences": entry.preferences.model_dump(mode="json"),
        "persisted": True,
    }


async def run_dialog(dialog, *, blocking=False, runtime=None):
    runtime = runtime if runtime is not None else ScenarioRuntime(dialog)
    result = DialogResult(dialog.scenario, dialog.label, blocking)
    result.budget_profile = runtime.budget_profile
    started = time.perf_counter()
    beginning = runtime.clock.now()
    responses = []
    next_turn_id = 1
    outbound = dialog.setup["channel"] == "outbound"
    try:
        async with runtime.application() as client:
            if outbound:
                opening = await runtime.start_outbound()
                if opening is None:
                    result.add("outbound_open", "Исполнитель не открыл исходящий диалог", "error")
                else:
                    responses.append(opening)
                    next_turn_id = opening.turn_id + 1
            for number, step in enumerate(dialog.steps, 1):
                if "patient" not in step:
                    if "event" in step:
                        if outbound and step["event"] == "voicemail":
                            from app.dialogue.outbound import OutboundEvent

                            reply = await runtime.service.outbound_event(
                                runtime.call_id,
                                OutboundEvent(
                                    attempt_id=runtime.reminder_attempt_id,
                                    turn_id=2,
                                    kind="voicemail",
                                ),
                            )
                            responses.append(reply)
                            result.steps.append(
                                {
                                    "step": number,
                                    "turn": 2,
                                    "phase": reply.phase,
                                    "code": reply.code,
                                    "options": len(reply.options),
                                    "event": "voicemail",
                                }
                            )
                            await verify_step(result, number, step, reply, runtime)
                        elif step["event"] in {"handoff_no_answer", "handoff_result"}:
                            outcome = step.get("outcome", "no_answer")
                            callback_reply = await record_fake_transfer(
                                result,
                                runtime,
                                outcome=outcome,
                                number=number,
                                start_callback=True,
                                responses=responses,
                                turn_id=responses[-1].turn_id + 1 if responses else 1,
                            )
                            result.steps.append(
                                {
                                    "step": number,
                                    "event": "handoff_result",
                                    "outcome": outcome,
                                    "evidence": "fake_transfer",
                                }
                            )
                            if callback_reply is not None:
                                responses.append(callback_reply)
                                next_turn_id = max(next_turn_id, callback_reply.turn_id + 1)
                                result.steps[-1].update(
                                    {
                                        "turn": callback_reply.turn_id,
                                        "phase": callback_reply.phase,
                                        "code": callback_reply.code,
                                    }
                                )
                                await verify_step(result, number, step, callback_reply, runtime)
                        elif step["event"] == "handoff_connected":
                            await record_fake_transfer(
                                result,
                                runtime,
                                outcome="connected",
                                number=number,
                                start_callback=False,
                                responses=responses,
                            )
                            result.steps.append(
                                {
                                    "step": number,
                                    "event": "handoff_result",
                                    "outcome": "connected",
                                    "evidence": "fake_transfer",
                                }
                            )
                        else:
                            result.add(
                                "event:" + step["event"],
                                "Событие отсутствует в текстовом API",
                                "unsupported",
                                number,
                            )
                    # Приветствие браузера не подменяем вымышленным ответом сервера.
                    continue
                if step.get("advance_s"):
                    runtime.clock.value += timedelta(seconds=step["advance_s"])
                turn = next_turn_id
                next_turn_id += 1
                mutations_before = (
                    runtime.emulator.write_count,
                    runtime.emulator.patch_count,
                    runtime.emulator.client_patch_count,
                    runtime.emulator.escalation_count,
                )
                requests_before = len(runtime.emulator.requests)
                body = {
                    "turn_id": turn,
                    "text": step["patient"],
                    "is_final": step.get("is_final", True),
                }
                response = await client.post(f"/api/dialog/{runtime.call_id}/turn", json=body)
                if response.status_code != 200:
                    result.add("http", f"HTTP {response.status_code}", "error", number)
                    continue
                reply = Reply.model_validate(response.json())
                responses.append(reply)
                state_response = await client.get(f"/api/dialog/{runtime.call_id}")
                state_response.raise_for_status()
                state = state_response.json()
                if state["phase"] != reply.phase or state["version"] != reply.version:
                    result.add(
                        "state_reply_mismatch",
                        "Ответ не соответствует сохранённому состоянию",
                        "critical",
                        number,
                    )
                if reply.call_id != runtime.call_id or reply.turn_id != turn:
                    result.add(
                        "reply_identity",
                        "Ответ относится к другому звонку/ходу",
                        "critical",
                        number,
                    )
                result.steps.append(
                    {
                        "step": number,
                        "turn": turn,
                        "phase": reply.phase,
                        "code": reply.code,
                        "options": len(reply.options),
                        "http_requests": len(runtime.emulator.requests) - requests_before,
                        "identified": state["client_id"] is not None,
                        "said": list(reply.utterances),
                    }
                )
                if (
                    reply.information is not None
                    and len(runtime.emulator.requests) != requests_before
                ):
                    verified_lab_lookup = (
                        reply.code == "INFO_GIVEN"
                        and reply.information.topic == "lab_status"
                        and len(runtime.emulator.requests) - requests_before == 1
                        and runtime.emulator.requests[-1].method == "GET"
                        and runtime.emulator.requests[-1].resource == "clients"
                    )
                    if not verified_lab_lookup:
                        result.add(
                            "information_mis_request",
                            "Общая справка потребовала запросов к МИС",
                            step=number,
                        )
                mutations_after = (
                    runtime.emulator.write_count,
                    runtime.emulator.patch_count,
                    runtime.emulator.client_patch_count,
                    runtime.emulator.escalation_count,
                )
                if mutations_after != mutations_before:
                    count_before_replay = len(runtime.emulator.requests)
                    replay = await client.post(f"/api/dialog/{runtime.call_id}/turn", json=body)
                    if (
                        replay.status_code != 200
                        or replay.json() != response.json()
                        or len(runtime.emulator.requests) != count_before_replay
                    ):
                        result.add(
                            "mutation_replay", "Повтор хода не идемпотентен", "critical", number
                        )
                consent = bool(
                    re.match(r"^\s*(да|подтверждаю|верно)(?:\W|$)", step["patient"], re.IGNORECASE)
                )
                previous_phase = responses[-2].phase if len(responses) > 1 else "ready"
                if mutations_after[:3] != mutations_before[:3] and (
                    not consent
                    or not body["is_final"]
                    or step.get("safety", {}).get("flags")
                    or previous_phase
                    not in {
                        "held",
                        "cancel_confirm",
                        "move_confirm",
                        "move_finish_confirm",
                        "remind_details",
                    }
                ):
                    result.add(
                        "mutation_without_consent",
                        "Изменение без явного финального согласия",
                        "critical",
                        number,
                    )
                await verify_step(result, number, step, reply, runtime)
            if outbound and runtime.reminder_attempt_id is not None:
                state = await runtime.dialogue_store.inspect(runtime.call_id)
                reminder_result = state.reminder_result
                if reminder_result is not None:
                    await runtime.reminder_executor.finished(
                        runtime.reminder_attempt_id,
                        reminder_result.outcome,
                        mis_status=reminder_result.mis_status,
                    )
                    await verify_reminder_result(result, runtime, reminder_result)
            verify_records(result, runtime, dialog.result.get("stand"))
            configured_outcome = dialog.result.get("fake_transfer")
            if configured_outcome:
                outcome = (
                    configured_outcome.get("outcome", "connected")
                    if isinstance(configured_outcome, dict)
                    else configured_outcome
                )
                await record_fake_transfer(
                    result,
                    runtime,
                    outcome=outcome,
                    number=len(dialog.steps) + 1,
                    start_callback=False,
                    responses=responses,
                )
            expected_code = dialog.result.get("code")
            if expected_code == "ESCALATED" and any(r.transfer is not None for r in responses):
                if "handoff_completion" not in result.evidence:
                    result.add(
                        "handoff_completion_T08_T04",
                        "В YAML не задан исход перевода; результат связи не подтверждён",
                        "unsupported",
                    )
            elif expected_code and expected_code not in {r.code for r in responses}:
                result.add("result_code", f"Не получен итоговый код {expected_code}")
            if "card" in dialog.result:
                card_response = next(
                    (r for r in reversed(responses) if r.transfer is not None), None
                )
                await verify_handoff_card(
                    result, runtime, card_response, dialog.result["card"], len(responses)
                )
            if "callback" in dialog.result:
                await verify_callback_task(result, runtime, dialog.result["callback"], responses)
            if "sms" in dialog.result:
                result.add(
                    "sms_delivery_T20",
                    "Нельзя подтвердить обязательный результат sms",
                    "unsupported",
                )
            if "waitlist" in dialog.result or expected_code == "WAITLISTED":
                await verify_waitlist(result, runtime, dialog.result.get("waitlist"))
            if "recording" in dialog.result:
                result.add(
                    "recording_engine_T13",
                    "Нельзя подтвердить обязательный результат recording",
                    "unsupported",
                )
            if "home_visit" in dialog.result:
                expected = dialog.result["home_visit"]
                request = await runtime.handoff_store.request_for_call(runtime.call_id)
                if request is None:
                    result.add("home_visit_T09", "Заявка на вызов не найдена в локальном хранилище")
                else:
                    try:
                        payload = runtime.handoffs.decrypt_home_visit_payload(request)
                    except (ValueError, TypeError):
                        payload = {}
                    if (
                        request.purpose != "home_visit"
                        or request.home_visit_state != "pending_confirmation"
                        or request.identity_status != "verified"
                        or request.client_id != runtime.client_ids[expected["client"]]
                        or payload.get("requested_date")
                        != datetime.strptime(expected["date"], "%d.%m.%Y").date().isoformat()
                        or request.priority != "P3"
                        or payload.get("reason") != expected["reason"]
                        or not payload.get("address")
                        or len(runtime.handoff_store.requests) != 1
                    ):
                        result.add(
                            "home_visit_T09", "Сохранённая заявка не совпадает с данными сценария"
                        )
            # Наличие флага opt-out не доказывает физическое прекращение записи аудио.
            if dialog.result.get("recording") is False:
                state = await runtime.dialogue_store.inspect(runtime.call_id)
                if not state.recording_opt_out:
                    result.add("recording_consent_not_saved", "Пожелание не сохранено", "critical")
            end = await client.post(f"/api/dialog/{runtime.call_id}/end")
            if end.status_code != 200 or end.json()["phase"] != "ended":
                result.add("end", "Разговор не завершён", "critical")
    except Exception as error:
        # Диагностика не включает текст driver exceptions и исходные данные запроса.
        result.add("runtime_error", type(error).__name__, "error")
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    result.simulated_seconds = (runtime.clock.now() - beginning).total_seconds()
    result.requests = runtime.emulator.request_summary()
    result.mutations = {
        "visits_created": runtime.emulator.write_count,
        "visits_patched": runtime.emulator.patch_count,
        "clients_patched": runtime.emulator.client_patch_count,
        "escalations_created": runtime.emulator.escalation_count,
    }
    return result.finish()
