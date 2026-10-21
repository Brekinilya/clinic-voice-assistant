"""Recognize simple help requests before transaction intent extraction."""

import re

_CAPABILITY = re.compile(
    r"\b(?:что\s+(?:вы|ты)\s+умеете|кто\s+(?:вы|ты)(?:\s+(?:такой|такая))?"
    r"|(?:вы|ты)\s+кто|что\s+(?:вы|ты)\s+такое"
    r"|как\s+(?:вас|тебя)\s+зовут|ваше\s+имя|кто\s+это)\b"
)
_BOOKING_GUIDANCE = re.compile(
    r"\bкак\s+(?:мне\s+|можно\s+|лучше\s+)?(?:записаться|записать\s+ся)\b"
    r"|\b(?:узнать|подскажите|расскажите)\b[^.!?]{0,50}\bкак\b[^.!?]{0,30}\bзаписаться\b"
)
_BOOKING_ABANDONED = re.compile(
    r"\bя\s+(?:просто|только)\s+(?:хочу\s+)?(?:спросить|узнать)\b"
    r"|\bя\s+(?:просто|только)\s+хотел[а]?\s+(?:спросить|узнать)\b"
)
_GENERAL_HELP = re.compile(r"\bсправочн\w*\s+информац\w*\b")
_UNSUPPORTED_SERVICE = re.compile(
    r"\b(?:массаж\w*|массажист\w*)\b.*\b(?:запис\w*|хочу|можно|нужен|нужна)\b"
    r"|\b(?:запис\w*|хочу|можно|нужен|нужна)\b.*\b(?:массаж\w*|массажист\w*)\b"
)


def classify_introduction(text: str) -> str | None:
    """Return a non-transactional help route for an opening/navigation question."""
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    if _UNSUPPORTED_SERVICE.search(normalized):
        return "unsupported_service"
    if _BOOKING_ABANDONED.search(normalized):
        return "booking_abandoned"
    if _BOOKING_GUIDANCE.search(normalized):
        return "booking_guidance"
    if _CAPABILITY.search(normalized):
        return "capabilities"
    if _GENERAL_HELP.search(normalized):
        return "general_help"
    return None
