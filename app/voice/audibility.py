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


WORD = re.compile(r"[а-яёa-z0-9]+", re.I)
# Связка между ответом шлюзу и командой: «да, слышу, но отмените запись»
JOINERS = {"но", "а", "и", "только", "так", "вот"}


def split_audibility(text: str) -> tuple[Literal["yes", "no"] | None, str]:
    """Ответ на «Вы меня слышите?» в начале фразы и всё, что сказано после него.

    Остаток — новая реплика для диалога; сам ответ шлюзу согласием на действие не считается.
    """
    words = list(WORD.finditer(text))
    for size in range(len(words), 0, -1):
        answer = audibility_answer(" ".join(w.group() for w in words[:size]))
        if answer is None:
            continue
        rest = words[size:]
        while rest and normalize(rest[0].group()) in JOINERS:
            rest = rest[1:]
        return answer, text[rest[0].start() :].strip() if rest else ""
    return None, text


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
