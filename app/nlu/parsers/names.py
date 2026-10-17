"""ФИО пациента в любом порядке и фамилия врача из справочника."""

import re
from functools import lru_cache

from app.nlu.lexicon import morph
from app.nlu.parsers.dates import MONTHS, lookup
from app.nlu.parsers.numbers import numeral

WORD = re.compile(r"[а-яё]+(?:-[а-яё]+)*")
STOP = {
    "меня",
    "зовут",
    "это",
    "я",
    "мой",
    "моя",
    "мое",
    "фамилия",
    "имя",
    "отчество",
    "пациент",
    "пациентка",
    "его",
    "ее",
    "её",
    "дата",
    "рождения",
    "родился",
    "родилась",
    "год",
    "года",
    "году",
    "г",
    "так",
    "вот",
    "ну",
    "да",
    "нет",
    "а",
    "и",
    "пожалуйста",
    "значит",
    "записан",
    "записана",
    "по",
    "буквам",
    "числа",
}


@lru_cache(maxsize=4096)
def _parses(word: str):
    return tuple(morph().parse(word))


def _tagged(word: str, tag: str):
    return [p for p in _parses(word) if tag in p.tag]


def parse_full_name(text: str) -> dict[str, tuple[str, str]]:
    """last_name, first_name, middle_name — в именительном падеже, с заглавной буквы."""
    words = [w for w in WORD.findall(text.lower()) if w not in STOP and not _is_date_word(w)]
    if not words:
        return {}

    patr = _best(words, "Patr", exclude=set())
    first = None
    if patr is not None and patr > 0 and _tagged(words[patr - 1], "Name"):
        first = patr - 1
    if first is None:
        # «Иванов» — тоже форма имени «Иван», поэтому берём слово, которое именем быть вероятнее
        first = _best(words, "Name", exclude={patr})
    if first is None:
        return {}

    taken = {first, patr}
    last = _surname_index(words, first, patr, taken)

    gender = _gender(words[first], "Name") or (
        _gender(words[patr], "Patr") if patr is not None else None
    )
    found = {"first_name": (_nominative(words[first], "Name", gender), words[first])}
    if patr is not None:
        found["middle_name"] = (_nominative(words[patr], "Patr", gender), words[patr])
    if last is not None:
        found["last_name"] = (_nominative(words[last], "Surn", gender), words[last])
    return found


def _best(words: list[str], tag: str, exclude: set) -> int | None:
    scored = [
        (sum(p.score for p in _tagged(w, tag)), i) for i, w in enumerate(words) if i not in exclude
    ]
    score, index = max(scored, default=(0, None))
    return index if score > 0 else None


def _surname_index(words: list[str], first: int, patr: int | None, taken: set) -> int | None:
    # «Фамилия Имя Отчество» или «Имя Отчество Фамилия»
    candidates = [first - 1, (patr if patr is not None else first) + 1]
    for i in candidates:
        if 0 <= i < len(words) and i not in taken and _tagged(words[i], "Surn"):
            return i
    for i in candidates:
        if (
            0 <= i < len(words)
            and i not in taken
            and not _tagged(words[i], "Name")
            and not _tagged(words[i], "Patr")
        ):
            return i
    return None


def _gender(word: str, tag: str) -> str | None:
    parses = _tagged(word, tag)
    return parses[0].tag.gender if parses else None


def _nominative(word: str, tag: str, gender: str | None) -> str:
    parses = _tagged(word, tag)
    if gender:
        parses = [p for p in parses if p.tag.gender == gender] or parses
    if any(p.tag.case == "nomn" for p in parses):
        return _title(word)
    for p in parses:
        nominative = p.inflect({"nomn"})
        if nominative:
            return _title(nominative.word)
    return _title(word)


def _title(word: str) -> str:
    return "-".join(part.capitalize() for part in word.split("-"))


def _is_date_word(word: str) -> bool:
    return numeral(word) is not None or lookup(word, MONTHS) is not None


def parse_doctor(text: str, surnames: list[str]) -> tuple[str, str] | None:
    """Фамилия врача из справочника, как её назвал пациент: «к Кузнецовой» → «Кузнецова»."""
    known = {s.lower().replace("ё", "е"): s for s in surnames}
    for word in WORD.findall(text.lower()):
        forms = [word]  # порядок важен: Волков и Волкова — разные врачи
        for p in _parses(word):
            if "Surn" in p.tag and (nominative := p.inflect({"nomn"})):
                forms.append(nominative.word)
        for form in forms:
            key = form.replace("ё", "е")
            if key in known:
                return known[key], word
    return None
