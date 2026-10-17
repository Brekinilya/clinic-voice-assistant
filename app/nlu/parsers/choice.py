"""Какой из предложенных вариантов выбрал пациент: «второй», «давайте пятницу», «где двенадцать ноль-ноль»."""

import re
from dataclasses import dataclass
from datetime import date, time

from app.nlu.dictionaries import specialty_lexicon
from app.nlu.lexicon import lemmas, normalize, tokenize
from app.nlu.parsers.dates import MONTHS, lookup, parse_dates
from app.nlu.parsers.names import parse_doctor, parse_full_name
from app.nlu.parsers.numbers import Number, spoken_numbers
from app.nlu.parsers.times import parse_times
from app.scheduling.matching import profile_key

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

    date_text = re.sub(r"\s+", " ", normalize(text))
    if ordinal:
        # «На первый вариант» — номер, а оставшееся «но в пятницу» — ограничение даты.
        date_text = date_text.replace(ordinal[1], "", 1)
    dates = parse_dates(date_text, today)
    if dates:

        def date_matches(option: Option) -> bool:
            if option.day is None:
                return False
            day = option.day.isoformat()
            return (
                ("date_from" not in dates or day >= dates["date_from"][0])
                and ("date_to" not in dates or day <= dates["date_to"][0])
                and option.day.isoweekday() not in dates.get("exclude_weekdays", ((), ""))[0]
                and day not in dates.get("exclude_dates", ((), ""))[0]
            )

        narrow({k for k, option in enumerate(options) if date_matches(option)}, text.strip())

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
        narrow(
            {
                k
                for k, o in enumerate(options)
                if o.specialty and profile_key(o.specialty) == profile_key(hit.key)
            },
            hit.phrase,
        )

    doctors = [o.doctor.split()[0] for o in options if o.doctor and o.doctor.strip()]
    if doctors and (named := parse_doctor(text, doctors)):
        name_parts = parse_full_name(text)

        def doctor_matches(option: Option) -> bool:
            if not option.doctor:
                return False
            parts = option.doctor.split()
            if normalize(parts[0]) != normalize(named[0]):
                return False
            # Имя и отчество врача могут разрешить однофамильцев. Это выбор из
            # предложенного списка; строгую идентификацию пациента не меняем.
            return all(
                field not in name_parts
                or len(parts) > index
                and normalize(parts[index]) == normalize(name_parts[field][0])
                for field, index in (("first_name", 1), ("middle_name", 2))
            )

        narrow(
            {k for k, option in enumerate(options) if doctor_matches(option)},
            named[1],
        )

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
