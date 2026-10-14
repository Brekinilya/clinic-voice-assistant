"""Локальный разбор фразы в сообщениях app/contracts.py: ответ на вопрос, интент, параметры, флаги."""

from collections.abc import Iterable
from datetime import date, time

from app.contracts import NluRequest, NluResult, NluSlots
from app.nlu.intents import classify
from app.nlu.parsers import Option, parse_answer, parse_digits, parse_expected
from app.policies.safety import check

DATE_SLOTS = {"date_from", "date_to", "birth_date"}
TIME_SLOTS = {"time_from", "time_to"}


class RulesNlu:
    """Разбор без внешних сервисов.

    Пустые intents и slots значат «правилами не понято» — фразу отдают общему разбору,
    а без него считают непонятой (R-05).
    """

    def __init__(self, doctor_names: Iterable[str] = ()):
        # ФИО врачей из справочника стенда; пациент называет врача по фамилии
        self.surnames = sorted({name.split()[0] for name in doctor_names if name.strip()})

    def parse(self, request: NluRequest) -> NluResult:
        options = [
            Option(day=o.day, at=o.at, doctor=o.doctor, specialty=o.specialty)
            for o in request.options
        ]
        parsed = parse_expected(
            request.text, request.expect, request.today, options=options, doctors=self.surnames
        )
        flags = check(request.text).flags
        slots = {name: _typed(name, value) for name, value in parsed.slots.items()}
        evidence = dict(parsed.evidence)

        if request.expect in {"free", "full_name"} and "policy_number" not in slots:
            # третий шаг идентификации: полис диктуют в ответ на обычный вопрос
            digits = parse_digits(request.text)
            if digits:
                slots["policy_number"] = digits
                evidence["policy_number"] = request.text.strip()

        topic = classify(request.text, flags, by_place=request.expect == "free")
        intents = [parsed.intent] if parsed.intent else []
        if topic and topic[0] not in intents:
            intents.append(topic[0])
        if request.expect == "free" and not intents:
            # «Нет, спасибо» на «Что-нибудь ещё?»; при названной цели короткий ответ лишний
            answer = parse_answer(request.text)
            if answer:
                intents.append(answer)

        alternatives = {}
        if "birth_date" in slots and parsed.alternatives.get("birth_date"):
            alternatives["birth_date"] = tuple(
                date.fromisoformat(d) for d in parsed.alternatives["birth_date"]
            )

        return NluResult(
            call_id=request.call_id,
            turn_id=request.turn_id,
            intents=tuple(intents),
            slots=NluSlots(**slots),
            flags=flags,
            evidence=evidence,
            alternatives=alternatives,
            source="rules",
        )


def _typed(name: str, value: object) -> object:
    if name in DATE_SLOTS:
        return date.fromisoformat(value)
    if name in TIME_SLOTS:
        return time.fromisoformat(value)
    if name == "exclude_weekdays":
        return tuple(value)
    return value
