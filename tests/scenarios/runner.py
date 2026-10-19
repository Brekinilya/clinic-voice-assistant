"""Выполняем YAML через HTTP API; ожидания не подставляются в NLU или ответы."""

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from app.dialogue.models import Reply, Turn
from app.handoffs.models import CallbackState
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
    "referral.found": {"offered"},
    "referral.done": {"booked"},
    "referral.missing": {"identified"},
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
}
CODE_EXPECTATIONS = {
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
    "referral.paper": {"TRANSFER_REQUESTED"},
}
GAPS = {
    "labs.": "labs_status_source_T09",
    "home_visit.": "home_visit_T08",
    "waitlist.": "waitlist_T08",
    "attach.": "faq_T09",
    "schedule.": "schedule_faq_T09",
    "silence.": "voice_event_channel_T03",
    "complaint.callback": "callback_T08",
    "transfer.outside_hours": "callback_T08",
    "my_visits.sms_sent": "sms_delivery_T20",
    "ident.relation": "guardian_T10",
    "ident.ask_child": "guardian_T10",
    "paid.price": "legacy_paid_catalog_outside_scope",
    "transfer.ministry": "faq_T09",
    "transfer.insurer": "faq_T09",
    "wait.schedule": "intermediate_wait_prompt_T03_T05",
}

TEXT_REQUIREMENTS = {
    "ident.ask_policy": r"полис",
    "reask.offer_operator": r"оператор|сотрудник|регистратор",
}

# Проверяем и происхождение, и содержание; одного INFO_GIVEN недостаточно.
INFORMATION_EXPECTATIONS = {
    "schedule.specialty": (
        "specialty_schedule",
        (r"Хирурги принимают по будням", r"сменн.*график", r"точное время зависит от врача"),
    ),
    "transfer.ministry": (
        "transfer_ministry",
        (r"льготн.*лекарств", r"колл-центр Минздрава", r"122"),
    ),
    "transfer.insurer": (
        "transfer_insurer",
        (
            r"Полис обязательного медицинского страхования",
            r"страховая компания",
            r"телефон указан на полисе",
        ),
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


def verify_step(result, number, step, response):
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
            if name == "referral.missing" and (
                response.code != "REFERRAL_REQUIRES_GP"
                or not re.search(r"подтвержд.*направлен", text)
                or not re.search(r"участков.*терапевт", text)
            ):
                result.add(
                    "referral_fallback",
                    "Не объяснено отсутствие подтверждённого допуска и вариант терапевта",
                    step=number,
                )
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
    if attempt.result != outcome:
        result.add("reminder_attempt", "Исполнитель не сохранил фактический исход звонка")
    client_id = runtime.client_ids[runtime.dialog.setup["call"]["client"]]
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
        contact = await runtime.reminder_store.contact(client_id)
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
    contact = await runtime.reminder_store.contact(client_id)
    result.evidence["outbound"] = {
        "path": "ReminderExecutor.tick → ReminderActionAdapter → DialogueService",
        "substitutions": [
            "телефонный originate/answer: ScenarioDialer",
            "HTTP МИС: ScenarioEmulator",
            "задания, попытки и отметки: MemoryReminderStore",
        ],
        "attempt_state": attempt.state,
        "outcome": outcome,
        "mis_status": reminder_result.mis_status,
        "mark_state": mark.state if mark is not None else None,
        "client_comment_patches": runtime.emulator.client_patch_count,
        "preserved_existing_comment": (
            client.comment is not None and client.comment.startswith("Прежняя заметка регистратора")
        ),
        "contact_opt_out": contact is not None and contact.opt_out_at is not None,
        "voicemail_event_port": outcome == "VOICEMAIL",
    }


async def verify_handoff_card(result, runtime, response, number):
    if response is None or response.transfer is None:
        return
    request = await runtime.handoff_store.request_for_call(runtime.call_id)
    if request is None:
        result.add("operator_card_T08", "HandoffService не сохранил карточку перевода", step=number)
        return
    if (request.route, request.priority) != (
        response.transfer.route,
        response.transfer.priority,
    ):
        result.add("operator_card_T08", "Карточка не совпадает с маршрутом в ответе", step=number)
        return
    result.evidence["handoff_card"] = {
        "route": request.route,
        "priority": request.priority,
        "source": request.source,
        "identity_status": request.identity_status,
        "transfer_state": request.transfer_state.value,
        "mis_state": request.mis_state.value,
    }


async def verify_callback_task(result, runtime, expectation):
    request = await runtime.handoff_store.request_for_call(runtime.call_id)
    task = (
        await runtime.handoff_store.get_callback_task_by_request(request.request_id)
        if request is not None
        else None
    )
    expected_state = (
        expectation.get("state", "queued") if isinstance(expectation, dict) else "queued"
    )
    if task is None:
        result.add("callback_task_T08", "HandoffService не сохранил задачу обратного звонка")
        return
    if task.state.value != expected_state or request.callback_state != CallbackState(
        expected_state
    ):
        result.add("callback_task_T08", "Состояние сохранённой задачи не совпадает с ожидаемым")
        return
    if not task.contact_masked or not task.contact_ciphertext or task.priority != request.priority:
        result.add("callback_task_T08", "В задаче не подтверждены номер и приоритет")
        return
    result.evidence["callback_task"] = {
        "state": task.state.value,
        "priority": task.priority,
        "contact_masked": task.contact_masked,
        "deadline": task.deadline.isoformat(),
    }


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


async def run_dialog(dialog, *, blocking=False, runtime=None):
    runtime = runtime if runtime is not None else ScenarioRuntime(dialog)
    result = DialogResult(dialog.scenario, dialog.label, blocking)
    result.budget_profile = runtime.budget_profile
    started = time.perf_counter()
    beginning = runtime.clock.now()
    responses = []
    outbound = dialog.setup["channel"] == "outbound"
    callback_active = False
    try:
        async with runtime.application() as client:
            if outbound:
                opening = await runtime.start_outbound()
                if opening is None:
                    result.add("outbound_open", "Исполнитель не открыл исходящий диалог", "error")
                else:
                    responses.append(opening)
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
                            verify_step(result, number, step, reply)
                        elif step["event"] == "handoff_no_answer":
                            request = await runtime.handoff_store.request_for_call(runtime.call_id)
                            if request is None:
                                result.add(
                                    "operator_card_T08",
                                    "Нет карточки перевода перед событием «нет ответа»",
                                    step=number,
                                )
                                continue
                            attempt_id = f"scenario-{number}"
                            event = SimpleNamespace(
                                attempt_id=attempt_id,
                                call_id=runtime.call_id,
                                route=request.route,
                                priority=request.priority,
                            )
                            await runtime.handoffs.transfer_started(event)
                            await runtime.handoffs.transfer_result(
                                SimpleNamespace(
                                    **vars(event), outcome="no_answer", reason="scenario"
                                )
                            )
                            turn_id = len(responses) + 1
                            callback_reply = await runtime.service.start_callback(
                                runtime.call_id, turn_id, "no_answer"
                            )
                            if callback_reply is None:
                                result.add(
                                    "callback_offer_T08",
                                    "HandoffService не предложил обратный звонок после отсутствия ответа",
                                    step=number,
                                )
                                continue
                            responses.append(callback_reply)
                            callback_active = True
                            result.steps.append(
                                {
                                    "step": number,
                                    "turn": turn_id,
                                    "phase": callback_reply.phase,
                                    "code": callback_reply.code,
                                    "event": "handoff_no_answer",
                                }
                            )
                            verify_step(result, number, step, callback_reply)
                        else:
                            result.add(
                                "event:" + step["event"],
                                "Событие отсутствует в текстовом API",
                                "unsupported",
                                number,
                            )
                    # Приветствие браузера не подменяем вымышленным ответом сервера.
                    continue
                if callback_active:
                    turn = len(responses) + 1
                    callback_reply = await runtime.handoffs.callback_turn(
                        runtime.call_id, Turn(turn_id=turn, text=step["patient"])
                    )
                    responses.append(callback_reply)
                    result.steps.append(
                        {
                            "step": number,
                            "turn": turn,
                            "phase": callback_reply.phase,
                            "code": callback_reply.code,
                            "options": len(callback_reply.options),
                        }
                    )
                    verify_step(result, number, step, callback_reply)
                    if callback_reply.phase == "ended":
                        callback_active = False
                    continue
                if step.get("advance_s"):
                    runtime.clock.value += timedelta(seconds=step["advance_s"])
                turn = len(responses) + 1
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
                    }
                )
                if (
                    reply.information is not None
                    and len(runtime.emulator.requests) != requests_before
                ):
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
                if "home_visit" in dialog.result and reply.transfer is not None:
                    result.add(
                        "home_visit_T08",
                        "Домашний визит пока передаётся сотруднику без завершённой заявки.",
                        "unsupported",
                        number,
                    )
                    break
                verify_step(result, number, step, reply)
                if "card" in dialog.result:
                    await verify_handoff_card(result, runtime, reply, number)
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
            expected_code = dialog.result.get("code")
            if expected_code == "ESCALATED" and any(r.transfer is not None for r in responses):
                request = await runtime.handoff_store.request_for_call(runtime.call_id)
                if request is None:
                    result.add(
                        "operator_card_T08",
                        "HandoffService не сохранил ожидаемую карточку перевода",
                    )
                else:
                    result.evidence["handoff_validation_scope"] = (
                        "Проверены локальная карточка и её маршрут; физическое соединение не проверялось."
                    )
            elif (
                expected_code
                and expected_code not in {r.code for r in responses}
                and not ("home_visit" in dialog.result and expected_code == "HOME_VISIT_REQUESTED")
            ):
                result.add("result_code", f"Не получен итоговый код {expected_code}")
            for key, feature in {
                "sms": "sms_delivery_T20",
                "home_visit": "home_visit_T08",
                "waitlist": "waitlist_T08",
                "recording": "recording_engine_T13",
            }.items():
                if key in dialog.result:
                    result.add(
                        feature, "Нельзя подтвердить обязательный результат " + key, "unsupported"
                    )
            if "callback" in dialog.result:
                await verify_callback_task(result, runtime, dialog.result["callback"])
            if "card" in dialog.result:
                await verify_handoff_card(
                    result,
                    runtime,
                    next((r for r in reversed(responses) if r.transfer is not None), None),
                    len(responses),
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
