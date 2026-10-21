"""Conservative routing for symptom statements not covered by a red-flag rule."""

import re

_DISTRESS = re.compile(
    r"\b(?:я\s+умираю|мне\s+(?:очень|ужасно|совсем|крайне)\s+плохо|"
    r"(?:стало|становится)\s+(?:очень\s+)?плохо)\b"
)
_PAIN = re.compile(
    r"\b(?:болит|болят|побаливает|побаливают|сильно\s+болит|больно)\b"
    r".{0,32}\b(?:живот|голова|грудь|сердце|спина|горло|рука|нога|глаз|ухо)\b"
    r"|\b(?:живот|голова|грудь|сердце|спина|горло|рука|нога|глаз|ухо)\b"
    r".{0,24}\b(?:болит|болят|побаливает|побаливают|больно)\b"
)
_ASSESSMENT_REQUEST = re.compile(
    r"\b(?:нужно|надо|стоит)\s+ли\s+(?:вызывать|звать)\s+(?:врача|доктора)\b"
)


def is_explicit_distress(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return bool(_DISTRESS.search(normalized))


def needs_urgent_screening(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return bool(
        _DISTRESS.search(normalized)
        or _PAIN.search(normalized)
        or _ASSESSMENT_REQUEST.search(normalized)
    )
