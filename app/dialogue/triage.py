"""Conservative routing for symptom statements not covered by a red-flag rule."""

import re

from app.policies.safety import is_explicit_distress as _is_explicit_distress

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
    return _is_explicit_distress(text)


def asks_whether_to_call_doctor(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return bool(_ASSESSMENT_REQUEST.search(normalized))


def needs_urgent_screening(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    return bool(
        _is_explicit_distress(normalized)
        or _PAIN.search(normalized)
        or _ASSESSMENT_REQUEST.search(normalized)
    )
