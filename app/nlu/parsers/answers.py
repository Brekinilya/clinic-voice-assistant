"""Короткий ответ на вопрос ассистента: yes, no, maybe, repeat, opt_out или None, если не уверены.

Перед изменением записи в стенде нужен однозначный ответ, поэтому «да, но…»
и «да» с новыми подробностями возвращают None — такую фразу разбирает общий разбор.
"""

import re

from app.nlu.lexicon import normalize

YES = {
    "да", "ага", "угу", "давайте", "давай", "хорошо", "ладно", "можно", "подходит", "устраивает",
    "правильно", "верно", "согласен", "согласна", "ок", "окей", "записывайте", "соединяйте",
    "переносите", "отменяйте", "отправьте", "отправляйте", "приду", "придем", "буду",
    "перезвоните", "перезвонят",
}
SOFT_YES = {"конечно", "разумеется", "естественно", "точно", "именно", "обязательно", "абсолютно"}
NO = {"нет", "неверно", "неправильно", "нельзя", "ничего", "отказываюсь"}
FILLERS = {
    "пожалуйста", "спасибо", "большое", "ну", "вот", "так", "же", "уж", "вроде", "все", "ой", "а", "и",
    "мне", "я", "вам", "это", "оно", "девушка", "слушаю", "в", "принципе", "целом", "очень", "пусть",
}
BUT = {"но", "только", "лучше", "однако", "хотя"}

PHRASES = [
    ("opt_out", r"\bне (звоните|надо (мне )?звонить|нужно (мне )?звонить|беспокойте)\b|\bобзвон|\bуберите (мой )?номер"),
    ("repeat", r"\b(повторите|повтори|не расслышал[аи]?|не понял[аи]?|ещ[её] раз|как вы сказали|что-что)\b"),
    ("maybe", r"\b(не знаю|может быть|наверное|постараюсь|как получится|посмотрим|не уверен[аы]?|пока не решил[аи]?)\b"),
    ("no", r"\b(не надо|не нужно|не подходит|не устраивает|не хочу|не буду|не приду|не она|не он|не я|не туда|не так)\b"),
]
YES_PHRASES = r"\b(нет проблем|без проблем|не против|не возражаю)\b"
WORD = re.compile(r"[а-яa-z0-9]+")


def parse_answer(text: str) -> str | None:
    norm = normalize(text)
    words = WORD.findall(norm.replace("-", " "))
    if not words:
        return None
    if words in (["что"], ["как"], ["алло"], ["а"]):
        return "repeat"

    norm_yes = re.sub(YES_PHRASES, " согласен ", norm)
    for intent, pattern in PHRASES:
        if re.search(pattern, norm_yes):
            if intent == "no" and _starts_with_yes_then_no(words):
                return "no"
            return intent
    words = WORD.findall(norm_yes.replace("-", " "))

    yes = [w for w in words if w in YES]
    soft = [w for w in words if w in SOFT_YES]
    no = [w for w in words if w in NO]
    rest = [w for w in words if w not in YES | SOFT_YES | NO | FILLERS]

    if no:
        # «Нет, скажите, что можно…» — ответ, который начинается с «нет», это отказ
        if yes and words[0] != "нет" and not _starts_with_yes_then_no(words):
            return None
        return "no"
    if yes or soft:
        if rest or BUT & set(words):
            return None
        return "yes"
    return None


def _starts_with_yes_then_no(words: list[str]) -> bool:
    # «да нет, не надо» — это отказ
    return len(words) > 1 and words[0] == "да" and words[1] in {"нет", "не"}
