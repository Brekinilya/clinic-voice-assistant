"""Узкий ответ на проверку слышимости; никогда не служит согласием на действие МИС."""

import re
from typing import Literal

from app.nlu.lexicon import normalize

POSITIVE = re.compile(
    r"(?:да\s+)*(?:я\s+)?(?:вас\s+)?(?:(?:хорошо|отлично|нормально)\s+)?"
    r"слышу(?:\s+(?:хорошо|отлично|нормально))?(?:\s+(?:да|спасибо))*"
)
NEGATIVE = re.compile(
    r"(?:(?:да|нет)\s+)*(?:я\s+)?(?:вас\s+)?"
    r"(?:(?:не|плохо)\s+(?:слышу|слышно)|(?:слышу|слышно)\s+плохо)(?:\s+вас)?"
)


def audibility_answer(text: str) -> Literal["yes", "no"] | None:
    words = " ".join(re.findall(r"[а-яa-z0-9]+", normalize(text)))
    # Только целая фраза: «да, но отмените запись» остаётся новой командой.
    if re.fullmatch(r"(?:да|ага|угу)(?:\s+(?:да|ага|угу|спасибо))*", words):
        return "yes"
    if re.fullmatch(r"нет(?:\s+(?:нет|спасибо))*", words) or NEGATIVE.fullmatch(words):
        return "no"
    if POSITIVE.fullmatch(words):
        return "yes"
    return None
