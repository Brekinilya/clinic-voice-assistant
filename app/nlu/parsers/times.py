"""Желаемое время приёма: «после трёх», «до одиннадцати», «в десять тридцать», «с утра», «пораньше»."""

from dataclasses import dataclass
from functools import lru_cache

from app.nlu.dictionaries import SYNONYMS_PATH, day_part_lexicon, read_yaml
from app.nlu.lexicon import lemmas, tokenize
from app.nlu.parsers.dates import DIGIT_DATE, MONTHS, lookup
from app.nlu.parsers.numbers import Number, numeral, spoken_numbers

FROM_WORDS = {"после", "позже", "позднее"}
TO_WORDS = {"до", "раньше", "ранее"}
AT_WORDS = {"в", "к", "на", "около", "часов", "часам", "примерно"}
SKIP = {"часов", "часа", "час", "минут"}


@dataclass(frozen=True)
class Clock:
    hour: int
    minute: int
    start: int
    end: int

    def text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@lru_cache(maxsize=1)
def _day_parts():
    sections = read_yaml(SYNONYMS_PATH)["day_parts"]
    return day_part_lexicon(), {name: data["slots"] for name, data in sections.items()}


def parse_times(text: str, bare: bool = False) -> dict[str, tuple[object, str]]:
    """time_from, time_to, time_pref — значение и фраза-основание.

    bare — понимать и время без предлога («десять тридцать»), например при выборе из вариантов.
    """
    # Разделитель токенов убирает точки: иначе 16.10.2026 превращается во время
    # 16:10 и незаметно добавляет ложное ограничение к уже разобранной дате.
    without_dates = DIGIT_DATE.sub(
        lambda match: (
            " "
            if 1 <= int(match.group(1)) <= 31 and 1 <= int(match.group(2)) <= 12
            else match.group(0)
        ),
        text,
    )
    tokens = tokenize(without_dates)
    numbers = spoken_numbers(tokens)
    found: dict[str, tuple[object, str]] = {}

    for i, token in enumerate(tokens):
        if token in {"с", "от"} and (start := read_clock(tokens, numbers, i + 1)):
            found["time_from"] = (start.text(), _span(tokens, i, start.end))
            j = start.end
            if (
                j < len(tokens)
                and tokens[j] in {"до", "по"}
                and (end := read_clock(tokens, numbers, j + 1))
            ):
                found["time_to"] = (end.text(), _span(tokens, i, end.end))
        elif (
            token in FROM_WORDS
            and "time_from" not in found
            and (clock := read_clock(tokens, numbers, i + 1))
        ):
            if not _negated(tokens, i):
                found["time_from"] = (clock.text(), _span(tokens, i, clock.end))
            else:
                found["time_to"] = (clock.text(), _span(tokens, i - 1, clock.end))
        elif (
            token in TO_WORDS
            and "time_to" not in found
            and (clock := read_clock(tokens, numbers, i + 1))
        ):
            if not _negated(tokens, i):
                found["time_to"] = (clock.text(), _span(tokens, i, clock.end))
            else:
                found["time_from"] = (clock.text(), _span(tokens, i - 1, clock.end))
        elif token in AT_WORDS and not found and (clock := read_clock(tokens, numbers, i + 1)):
            evidence = _span(tokens, i, clock.end)
            found["time_from"] = (clock.text(), evidence)
            found["time_to"] = (clock.text(), evidence)

    if bare and not found:
        clock = _bare_clock(tokens, numbers)
        if clock:
            evidence = _span(tokens, clock.start, clock.end)
            found["time_from"] = (clock.text(), evidence)
            found["time_to"] = (clock.text(), evidence)

    normalized = " ".join(tokens)
    if "до обеда" in normalized and "time_to" not in found:
        found["time_to"] = ("12:00", "до обеда")

    lexicon, slots = _day_parts()
    for hit in lexicon.find(text):
        if hit.key == "day" and "до обеда" in normalized and hit.phrase == "обеда":
            continue
        for name, value in slots[hit.key].items():
            # явно названное время точнее, чем «утром» или «вечером»
            found.setdefault(name, (value, hit.phrase))
    return found


def read_clock(tokens: list[str], numbers: list[Number], i: int) -> Clock | None:
    """Время, которое начинается со слова i: «трёх», «десять тридцать», «полдесятого», «час дня»."""
    start = i
    while i < len(tokens) and tokens[i] in SKIP - {"час"}:
        i += 1
    if i >= len(tokens):
        return None

    token = tokens[i]
    if token == "полдень":
        return Clock(12, 0, start, i + 1)
    half = _half_past(tokens, i)
    if half:
        hour, end = half
        return _with_modifier(tokens, hour, 30, start, end)
    if token in {"час", "часа"} and i == start:
        return _with_modifier(tokens, 1, 0, start, i + 1)

    number = next((n for n in numbers if n.start == i), None)
    if not number or number.ordinal or not 0 <= number.value <= 24:
        return None
    if _followed_by_date_word(tokens, number.end):
        return None
    minute, end = 0, number.end
    nxt = next((n for n in numbers if n.start == number.end), None)
    if (
        nxt
        and not nxt.ordinal
        and 0 <= nxt.value <= 59
        and not _followed_by_date_word(tokens, nxt.end)
    ):
        minute, end = nxt.value, nxt.end
    while end < len(tokens) and tokens[end] in SKIP:
        end += 1
    return _with_modifier(tokens, number.value, minute, start, end)


def _half_past(tokens: list[str], i: int) -> tuple[int, int] | None:
    token = tokens[i]
    if token.startswith("пол") and len(token) > 3 and (word := numeral(token[3:])) and word[2]:
        return word[0] - 1, i + 1
    if (
        "половина" in lemmas(token)
        and i + 1 < len(tokens)
        and (word := numeral(tokens[i + 1]))
        and word[2]
    ):
        return word[0] - 1, i + 2
    return None


def _with_modifier(tokens: list[str], hour: int, minute: int, start: int, end: int) -> Clock:
    modifier = tokens[end] if end < len(tokens) else ""
    if modifier in {"дня", "вечера"} and hour < 12:
        hour += 12
        end += 1
    elif modifier in {"утра", "утром"}:
        end += 1
    elif 1 <= hour <= 6:
        # поликлиника работает с 7:30 до 20:00: «в три» — это 15:00
        hour += 12
    return Clock(hour % 24, minute, start, end)


def _bare_clock(tokens: list[str], numbers: list[Number]) -> Clock | None:
    for a, b in zip(numbers, numbers[1:]):
        if (
            a.end == b.start
            and not a.ordinal
            and not b.ordinal
            and 0 <= a.value <= 23
            and 0 <= b.value <= 59
        ):
            if not _followed_by_date_word(tokens, b.end):
                return _with_modifier(tokens, a.value, b.value, a.start, b.end)
    return None


def _followed_by_date_word(tokens: list[str], i: int) -> bool:
    return i < len(tokens) and bool(lookup(tokens[i], MONTHS))


def _negated(tokens: list[str], i: int) -> bool:
    return i > 0 and tokens[i - 1] == "не"


def _span(tokens: list[str], start: int, end: int) -> str:
    return " ".join(tokens[max(0, start) : end])
