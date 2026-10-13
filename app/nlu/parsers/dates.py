"""Даты: дата рождения и желаемые дни приёма относительно даты звонка (приложение А.3)."""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.nlu.lexicon import lemmas, tokenize
from app.nlu.parsers.numbers import Number, spoken_numbers

MAX_AGE = 110

MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
WEEKDAYS = {
    "понедельник": 1, "вторник": 2, "среда": 3, "четверг": 4, "пятница": 5, "суббота": 6, "воскресенье": 7,
}
RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
NEGATIONS = {"не", "кроме", "без"}
BOUNDS = {"после": "after", "с": "from", "со": "from", "от": "from", "до": "to", "по": "to"}
DIGIT_DATE = re.compile(r"(?<![\d:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])")


@dataclass(frozen=True)
class BirthDate:
    value: date
    alternatives: tuple[date, ...] = ()  # другое столетие для двузначного года
    evidence: str = ""


def lookup(token: str, table: dict[str, int]) -> int | None:
    for form in lemmas(token):
        if form in table:
            return table[form]
    return None


def parse_birth_date(text: str, today: date) -> BirthDate | None:
    tokens = tokenize(text)
    numbers = spoken_numbers(tokens)
    month_at = next((i for i, t in enumerate(tokens) if lookup(t, MONTHS)), None)

    if month_at is not None:
        day = next((n for n in numbers if n.end == month_at), None)
        year = next((n for n in numbers if n.start == month_at + 1), None)
        if not day or not year:
            return None
        month = lookup(tokens[month_at], MONTHS)
        start, end = day.start, year.end
    else:
        triple = _first_triple(numbers)
        if not triple:
            return None
        day, month_number, year = triple
        month = month_number.value
        start, end = day.start, year.end

    dates = []
    for y in _years(year.value):
        born = _safe_date(y, month, day.value)
        if born and _plausible(born, today):
            dates.append(born)
    if not dates:
        return None
    return BirthDate(dates[0], tuple(dates[1:]), " ".join(tokens[start:end]))


def _first_triple(numbers: list[Number]) -> tuple[Number, Number, Number] | None:
    for a, b, c in zip(numbers, numbers[1:], numbers[2:]):
        if a.end == b.start and b.end == c.start and 1 <= a.value <= 31 and 1 <= b.value <= 12:
            return a, b, c
    return None


def _years(value: int) -> list[int]:
    if value >= 1000:
        return [value]
    if value < 100:
        # двузначный год: сначала текущее столетие, затем прошлое (пациенту не больше 110 лет)
        return [2000 + value, 1900 + value]
    return []


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _plausible(born: date, today: date) -> bool:
    return born <= today and today.year - born.year <= MAX_AGE


def parse_dates(text: str, today: date) -> dict[str, tuple[object, str]]:
    """Желаемый период приёма: date_from, date_to, exclude_weekdays — значение и фраза-основание."""
    tokens = tokenize(text)
    numbers = spoken_numbers(tokens)
    days: list[tuple[date, date, str]] = []
    weeks: list[tuple[date, date, str]] = []
    weekdays: list[tuple[int, list[str], str]] = []
    excluded: list[tuple[int, str]] = []

    for i, token in enumerate(tokens):
        if (shift := RELATIVE_DAYS.get(token)) is not None:
            d = today + timedelta(days=shift)
            days.append((d, d, token))

        weekday = lookup(token, WEEKDAYS)
        if weekday:
            before = tokens[max(0, i - 2):i]
            if NEGATIONS & set(before):
                excluded.append((weekday, " ".join(before + [token])))
            else:
                weekdays.append((weekday, before, " ".join(before[-1:] + [token])))

        if "неделя" in lemmas(token):
            span = _week(today, tokens, i, numbers)
            if span:
                (weeks if span[0] != span[1] else days).append(span)

        if token == "выходных" and i > 0 and tokens[i - 1] == "после":
            monday = today + timedelta(days=7 - today.weekday())
            days.append((monday, monday, "после выходных"))

        if token in {"день", "дня", "дней"} and i > 1 and tokens[i - 2] == "через":
            n = next((n for n in numbers if n.end == i), None)
            if n:
                d = today + timedelta(days=n.value)
                days.append((d, d, " ".join(tokens[i - 2:i + 1])))

    if weekdays and weeks:
        # «на следующей неделе в среду» — день недели внутри названной недели
        monday = weeks[0][0] - timedelta(days=weeks[0][0].weekday())
        for weekday, _, evidence in weekdays:
            d = monday + timedelta(days=weekday - 1)
            if d >= today:
                days.append((d, d, f"{weeks[0][2]}, {evidence}"))
    else:
        days += weeks
        for weekday, before, evidence in weekdays:
            d = _weekday_date(today, weekday, before)
            days.append((d, d, evidence))

    calendar = _calendar_dates(tokens, numbers, today)
    days += [(d, d, e) for kind, d, e in calendar if kind == "day"]
    days += _digit_dates(text, today)
    lower = [(d, e) for kind, d, e in calendar if kind == "from"]
    upper = [(d, e) for kind, d, e in calendar if kind == "to"]

    date_from = min((d for d, _, _ in days), default=None)
    date_to = max((d for _, d, _ in days), default=None)
    if lower:
        date_from = max([d for d, _ in lower] + [d for d in [date_from] if d])
    if upper:
        date_to = min([d for d, _ in upper] + [d for d in [date_to] if d])

    found: dict[str, tuple[object, str]] = {}
    evidence = ", ".join([e for _, _, e in days] + [e for _, e in lower + upper])
    if date_from:
        found["date_from"] = (date_from.isoformat(), evidence)
    if date_to:
        found["date_to"] = (date_to.isoformat(), evidence)
    if excluded:
        found["exclude_weekdays"] = (sorted({w for w, _ in excluded}), ", ".join(e for _, e in excluded))
    return found


def _weekday_date(today: date, weekday: int, before: list[str]) -> date:
    ahead = (weekday - today.isoweekday()) % 7 or 7  # сегодняшний день недели — уже следующий
    d = today + timedelta(days=ahead)
    modifiers = {f for t in before for f in lemmas(t)}
    if "следующий" in modifiers or "будущий" in modifiers:
        next_monday = today + timedelta(days=7 - today.weekday())
        d = next_monday + timedelta(days=weekday - 1)
    elif "этот" in modifiers:
        this_monday = today - timedelta(days=today.weekday())
        candidate = this_monday + timedelta(days=weekday - 1)
        d = candidate if candidate >= today else d
    return d


def _week(today: date, tokens: list[str], i: int, numbers: list[Number]) -> tuple[date, date, str] | None:
    before = tokens[max(0, i - 2):i]
    modifiers = {f for t in before for f in lemmas(t)}
    monday = today - timedelta(days=today.weekday())
    if "через" in before:
        n = next((n.value for n in numbers if n.end == i), 1)
        d = today + timedelta(days=7 * n)
        return d, d, " ".join(before + [tokens[i]])
    if "следующий" in modifiers or "будущий" in modifiers:
        start = monday + timedelta(days=7)
        return start, start + timedelta(days=6), " ".join(before + [tokens[i]])
    if "этот" in modifiers:
        return today, monday + timedelta(days=6), " ".join(before + [tokens[i]])
    return None


def _calendar_dates(tokens: list[str], numbers: list[Number], today: date) -> list[tuple[str, date, str]]:
    """Числа месяца: («day» | «from» | «to», дата, фраза). «после двадцатого» — с 21-го."""
    found = []
    for n in numbers:
        if not 1 <= n.value <= 31:
            continue
        before = tokens[n.start - 1] if n.start > 0 else ""
        month = lookup(tokens[n.end], MONTHS) if n.end < len(tokens) else None
        if month:
            d, end = _future(today, month, n.value), n.end + 1
        elif n.ordinal and (before == "на" or before in BOUNDS or _next_is(tokens, n.end, "число")):
            d, end = _future(today, today.month, n.value, roll_month=True), n.end
        else:
            continue
        if not d:
            continue
        kind = BOUNDS.get(before, "day")
        if kind == "after":
            kind, d = "from", d + timedelta(days=1)
        start = n.start - 1 if before in BOUNDS or before == "на" else n.start
        found.append((kind, d, " ".join(tokens[start:end])))
    return found


def _next_is(tokens: list[str], i: int, lemma: str) -> bool:
    return i < len(tokens) and lemma in lemmas(tokens[i])


def _future(today: date, month: int, day: int, roll_month: bool = False) -> date | None:
    d = _safe_date(today.year, month, day)
    if d and d >= today:
        return d
    if roll_month:
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return _safe_date(year, month, day)
    return _safe_date(today.year + 1, month, day)


def _digit_dates(text: str, today: date) -> list[tuple[date, date, str]]:
    found = []
    for m in DIGIT_DATE.finditer(text):
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and (d := _future(today, month, day)):
            found.append((d, d, m.group(0)))
    return found
