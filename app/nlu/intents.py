"""Интент по правилам: слова из приложения А.2 и флаги safety.check, без общего разбора фразы.

Ничего не найдено — это не «не понял», а повод отдать фразу общему разбору.
"""

from app.nlu.dictionaries import action_lexicon, service_lexicon, specialty_lexicon
from app.nlu.lexicon import Hit

# Флаги безопасности задают интент сильнее любых слов
FLAG_INTENTS = (("red_a", "I-11"), ("complaint", "I-12"), ("medical", "I-13"))

# Если в фразе слова нескольких интентов, побеждает первый по списку:
# «записана на четверг, хочу перенести» — перенос, «нет талонов, запишите в очередь» — лист ожидания
INTENT_ORDER = (
    "I-13",
    "I-09",
    "I-02",
    "I-03",
    "I-05",
    "I-07",
    "I-08",
    "I-04",
    "I-14",
    "I-10",
    "I-06",
    "I-01",
)


def classify(
    text: str, flags: frozenset[str] = frozenset(), by_place: bool = True
) -> tuple[str, str] | None:
    """Код интента I-01…I-14 и фраза-основание или None.

    by_place — считать записью фразу, где назван только врач или кабинет. В ответе на вопрос
    («которая к лору?») это выбор варианта, а не новая запись.
    """
    for flag, intent in FLAG_INTENTS:
        if flag in flags:
            return intent, flag

    hits = {hit.key: hit for hit in action_lexicon().find(text)}
    for intent in INTENT_ORDER:
        if intent in hits:
            return intent, hits[intent].phrase

    if not by_place:
        return None
    # «Мне к кардиологу нужно», «Нужно сделать УЗИ» — врач или кабинет без глагола: запись
    where: Hit | None = specialty_lexicon().best(text) or service_lexicon().best(text)
    if where:
        return "I-01", where.phrase
    return None
