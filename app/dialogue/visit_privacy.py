"""Локальная проверка, о чьих записях спрашивают; доверенность без данных МИС не предполагаем."""

import re
from typing import Literal


def requested_owner(text: str) -> Literal["self", "other", "unknown"]:
    # Это организационный запрет раскрытия, а не дополнение медицинского классификатора Ильи.
    normalized = text.casefold().replace("ё", "е")
    other = re.search(
        r"\b(?:мам(?:а|ы|е|у|ой)|пап(?:а|ы|е|у|ой)|жен(?:а|ы|е|у|ой)|"
        r"муж(?:а|у|ем)?|брат(?:а|у|ом)?|сестр(?:а|ы|е|у|ой)|"
        r"доч(?:ь|ери|ку|ка)|сын(?:а|у|ом)?|ребен(?:ок|ка|ку)|"
        r"сосед(?:а|ки|ке|ку)?|подруг(?:а|и|е|у)|родственник(?:а|у|ом)?|"
        r"он|она|его|ее|чуж(?:ая|ую|ой))\b",
        normalized,
    )
    if other:
        return "other"
    if re.search(
        r"\b(?:я|мне|меня|мою|моя|мой|мои|мое|мои|свой|своя|свое|свои|свою|своего|своей)\b",
        normalized,
    ):
        return "self"
    return "unknown"


def asks_for_sms(text: str) -> bool:
    return re.search(r"\b(?:смс|sms|отправ\w*|пришл\w*)\b", text.casefold()) is not None


def mentions_existing_visits(text: str) -> bool:
    return (
        re.search(r"\b(?:записан\w*|запись|записи|записей|талон\w*)\b", text.casefold()) is not None
    )
