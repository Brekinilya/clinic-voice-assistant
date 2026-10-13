"""Какой из предложенных вариантов выбрал пациент: «второй», «давайте пятницу», «где двенадцать ноль-ноль»."""

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

from app.nlu.dictionaries import specialty_lexicon
from app.nlu.lexicon import lemmas, normalize, tokenize
from app.nlu.parsers.dates import MONTHS, RELATIVE_DAYS, WEEKDAYS, lookup
from app.nlu.parsers.names import parse_doctor
from app.nlu.parsers.numbers import Number, spoken_numbers
from app.nlu.parsers.times import parse_times

LAST = {"последний", "крайний"}
ANY = {"любой", "разница"}
POINTERS = {"номер", "вариант"}
# «первый не подходит» — это отказ, а не выбор
REJECTION = re.compile(
    r"\bне (подходит|подходят|устраивает|устраивают|получится|смогу|могу|надо|нужно|хочу)\b"
)


@dataclass(frozen=True)
class Option:
    """Вариант, который назвал ассистент: время приёма или существующая запись."""

    day: date | None = None
    at: time | None = None
    doctor: str | None = None
    specialty: str | None = None


def parse_choice(text: str, options: list[Option], today: date) -> tuple[int, str] | None:
    """Номер варианта с единицы и фраза-основание; None — если выбор не однозначен."""
    if not options or REJECTION.search(normalize(text)):
        return None
    tokens = tokenize(text)
    numbers = spoken_numbers(tokens)
    candidates = set(range(len(options)))
    evidence: list[str] = []

    def narrow(matching: set[int], phrase: str):
        nonlocal candidates
        candidates &= matching
        evidence.append(phrase)

    ordinal = _ordinal(tokens, numbers, len(options))
    if ordinal:
        narrow({ordinal[0]}, ordinal[1])

    for i, token in enumerate(tokens):
        weekday = lookup(token, WEEKDAYS)
        if weekday and not (i > 0 and tokens[i - 1] in {"не", "кроме"}):
            narrow(
                {k for k, o in enumerate(options) if o.day and o.day.isoweekday() == weekday}, token
            )
        if token in RELATIVE_DAYS:
            d = today + timedelta(days=RELATIVE_DAYS[token])
            narrow({k for k, o in enumerate(options) if o.day == d}, token)

    for n in numbers:
        if _is_day_of_month(tokens, n, len(options)):
            phrase = " ".join(tokens[n.start : n.end])
            narrow({k for k, o in enumerate(options) if o.day and o.day.day == n.value}, phrase)

    times = parse_times(text, bare=True)
    if "time_pref" in times and "time_from" not in times:
        # «который пораньше» — самый ранний из названных, «попозже» — самый поздний
        timed = sorted((o.day, o.at, k) for k, o in enumerate(options) if o.day and o.at)
        if timed:
            pick = timed[0] if times["time_pref"][0] == "early" else timed[-1]
            narrow({pick[2]}, times["time_pref"][1])
    if "time_from" in times or "time_to" in times:
        low = times.get("time_from", ("00:00", ""))[0]
        high = times.get("time_to", ("23:59", ""))[0]
        if low == high:
            matching = {k for k, o in enumerate(options) if o.at and _same_clock(o.at, low)}
        else:
            matching = {
                k for k, o in enumerate(options) if o.at and low <= o.at.strftime("%H:%M") <= high
            }
        narrow(matching, next(phrase for _, phrase in times.values()))

    if any(o.specialty for o in options) and (hit := specialty_lexicon().best(text)):
        narrow({k for k, o in enumerate(options) if o.specialty == hit.key}, hit.phrase)

    doctors = [o.doctor for o in options if o.doctor]
    if doctors and (named := parse_doctor(text, doctors)):
        narrow({k for k, o in enumerate(options) if o.doctor == named[0]}, named[1])

    if not evidence and ANY & {f for t in tokens for f in lemmas(t)}:
        return 1, text.strip()
    if len(candidates) == 1 and evidence:
        return candidates.pop() + 1, ", ".join(evidence)
    return None


def _ordinal(tokens: list[str], numbers: list[Number], count: int) -> tuple[int, str] | None:
    for token in tokens:
        if lemmas(token) & LAST:
            return count - 1, token
    for n in numbers:
        if not 1 <= n.value <= count or _month_after(tokens, n):
            continue
        before = tokens[n.start - 1] if n.start > 0 else ""
        after = tokens[n.end] if n.end < len(tokens) else ""
        if n.ordinal or before in POINTERS or after in POINTERS:
            start = n.start - 1 if before in POINTERS else n.start
            end = n.end + 1 if after in POINTERS else n.end
            return n.value - 1, " ".join(tokens[start:end])
    return None


def _is_day_of_month(tokens: list[str], n: Number, count: int) -> bool:
    if not 1 <= n.value <= 31:
        return False
    if _month_after(tokens, n):
        return True
    # «второго» при двух вариантах — скорее номер варианта; «пятого» — число месяца
    return n.ordinal and n.value > count


def _month_after(tokens: list[str], n: Number) -> bool:
    return n.end < len(tokens) and lookup(tokens[n.end], MONTHS) is not None


def _same_clock(at: time, hhmm: str) -> bool:
    hour, minute = map(int, hhmm.split(":"))
    return at.minute == minute and at.hour in {hour, (hour + 12) % 24, (hour - 12) % 24}
