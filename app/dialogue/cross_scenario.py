"""Short utterances that should work at every active dialogue question."""

import re
from enum import StrEnum


class CrossScenario(StrEnum):
    WAIT = "wait"
    GOODBYE = "goodbye"
    RESET = "reset"
    MORE_OPTIONS = "more_options"
    REPEAT = "repeat"
    UNSURE = "unsure"
    FILLER = "filler"
    THIRD_PARTY = "third_party"
    SWITCH_CANCEL = "switch_cancel"
    SWITCH_VISITS = "switch_visits"


_PATTERNS = (
    (CrossScenario.THIRD_PARTY, re.compile(r"\b(для мам|для ребен|для ребен|не для меня|за мам)\w*\b")),
    (CrossScenario.SWITCH_CANCEL, re.compile(r"\b(отмените|отменить|отменяйте)\b.*\b(запис|прием|талон)\w*\b")),
    (CrossScenario.SWITCH_VISITS, re.compile(r"\b(мои записи|какие у меня записи|покажите записи|мои приемы)\b")),
    (CrossScenario.GOODBYE, re.compile(r"\b(до свидания|всего доброго|заканчиваем)\b")),
    (CrossScenario.WAIT, re.compile(r"\b(подождите|подожди|минутку|секундочку)\b")),
    (CrossScenario.RESET, re.compile(r"\b(передумал[аи]?|давайте сначала|начн[её]м сначала|сначала)\b")),
    (
        CrossScenario.MORE_OPTIONS,
        re.compile(r"\b(еще|другие|какие|вариант)\b.*\bвариант(ы|ов)?\b|\bварианты\b"),
    ),
    (CrossScenario.MORE_OPTIONS, re.compile(r"\b(другой|другая|другое)\s+(день|дата|время)\b")),
    (
        CrossScenario.REPEAT,
        re.compile(
            r"^\s*(?:(?:я\s+)?не расслышал[аи]?(?:,?\s*что вы сказали)?|"
            r"повторите(?:,?\s*пожалуйста)?|повтори(?:,?\s*пожалуйста)?|"
            r"что|как|алло|ещё раз)\s*[.!?]*\s*$"
        ),
    ),
    (CrossScenario.UNSURE, re.compile(r"\b(не знаю|ну да,? наверное|может быть|наверное)\b")),
    (CrossScenario.FILLER, re.compile(r"^(мм+|эм+|м-м+|а-а+|ну\.?)$")),
)


def classify(text: str) -> CrossScenario | None:
    normalized = " ".join(text.casefold().replace("ё", "е").split()).strip(" .,!?")
    if normalized in {"пока", "ну пока", "все пока"}:
        return CrossScenario.GOODBYE
    return next((kind for kind, pattern in _PATTERNS if pattern.search(normalized)), None)
