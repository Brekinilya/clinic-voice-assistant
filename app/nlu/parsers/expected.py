"""Разбор ответа по тому, чего ждал ассистент (expect из шаблона реплики)."""

from dataclasses import dataclass, field
from datetime import date

from app.nlu.dictionaries import specialty_lexicon
from app.nlu.parsers.answers import parse_answer
from app.nlu.parsers.choice import Option, parse_choice
from app.nlu.parsers.dates import parse_birth_date, parse_dates
from app.nlu.parsers.names import parse_doctor, parse_full_name
from app.nlu.parsers.times import parse_times


@dataclass
class Parsed:
    intent: str | None = None
    slots: dict = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    alternatives: dict = field(default_factory=dict)  # например, дата рождения в другом столетии

    def put(self, found: dict[str, tuple[object, str]]):
        for name, (value, phrase) in found.items():
            self.slots[name] = value
            self.evidence[name] = phrase

    @property
    def empty(self) -> bool:
        return self.intent is None and not self.slots


def parse_expected(
    text: str,
    expect: str,
    today: date,
    options: list[Option] | None = None,
    doctors: list[str] | None = None,
) -> Parsed:
    """Быстрый разбор без внешних сервисов. Пустой результат — фразу нужно отдать общему разбору."""
    result = Parsed()

    if expect == "yes_no":
        result.intent = parse_answer(text)
        return result

    if expect == "choice":
        chosen = parse_choice(text, options or [], today)
        if chosen:
            result.intent = "choice"
            result.put({"choice_index": chosen})
        else:
            answer = parse_answer(text)
            result.intent = answer if answer in {"no", "repeat"} else None
        return result

    if expect in {"full_name", "birth_date"}:
        if expect == "full_name":
            result.put(parse_full_name(text))
        born = parse_birth_date(text, today)
        if born:
            result.put({"birth_date": (born.value.isoformat(), born.evidence)})
            if born.alternatives:
                result.alternatives["birth_date"] = [d.isoformat() for d in born.alternatives]
        return result

    result.put(parse_dates(text, today))
    result.put(parse_times(text, bare=expect == "date_time"))
    if expect == "free":
        hit = specialty_lexicon().best(text)
        if hit:
            result.put({"specialty": (hit.key, hit.phrase)})
        doctor = parse_doctor(text, doctors or [])
        if doctor:
            result.put({"doctor": doctor})
    return result
