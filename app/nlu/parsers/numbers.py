"""Числа словами и цифрами: «семьдесят восьмого», «ноль три», «две тысячи девятнадцатого», «1978»."""

from dataclasses import dataclass

from app.nlu.lexicon import lemmas, tokenize

ZERO, UNIT, TEEN, TEN, HUNDRED, THOUSAND = range(6)
RANK = {UNIT: 1, TEEN: 2, TEN: 2, HUNDRED: 3, THOUSAND: 4}

CARDINALS = {
    "ноль": 0, "нуль": 0,
    "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
    "тысяча": 1000,
}
ORDINALS = {
    "нулевой": 0,
    "первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5, "шестой": 6, "седьмой": 7,
    "восьмой": 8, "девятый": 9, "десятый": 10, "одиннадцатый": 11, "двенадцатый": 12, "тринадцатый": 13,
    "четырнадцатый": 14, "пятнадцатый": 15, "шестнадцатый": 16, "семнадцатый": 17, "восемнадцатый": 18,
    "девятнадцатый": 19, "двадцатый": 20, "тридцатый": 30, "сороковой": 40, "пятидесятый": 50,
    "шестидесятый": 60, "семидесятый": 70, "восьмидесятый": 80, "девяностый": 90,
    "сотый": 100, "двухсотый": 200, "трехсотый": 300, "четырехсотый": 400, "пятисотый": 500,
    "шестисотый": 600, "семисотый": 700, "восьмисотый": 800, "девятисотый": 900,
    "тысячный": 1000, "двухтысячный": 2000,
}


@dataclass(frozen=True)
class Number:
    value: int
    digits: str  # как продиктовано: «03» для «ноль три»
    start: int   # индексы слов, end не включается
    end: int
    ordinal: bool = False


def numeral(token: str) -> tuple[int, int, bool] | None:
    """Значение, разряд и порядковость одного слова-числительного."""
    forms = lemmas(token)
    for table, ordinal in ((CARDINALS, False), (ORDINALS, True)):
        for form in forms:
            if form in table:
                value = table[form]
                return value, _kind(value), ordinal
    return None


def _kind(value: int) -> int:
    if value == 0:
        return ZERO
    if value < 10:
        return UNIT
    if value < 20:
        return TEEN
    if value < 100:
        return TEN
    if value < 1000:
        return HUNDRED
    return THOUSAND


def spoken_numbers(tokens: list[str]) -> list[Number]:
    found: list[Number] = []
    group: dict | None = None

    def close():
        nonlocal group
        if group:
            digits = group.get("digits") or str(group["value"])
            found.append(Number(group["value"], digits, group["start"], group["end"], group["ordinal"]))
        group = None

    for i, token in enumerate(tokens):
        if token.isdigit():
            close()
            found.append(Number(int(token), token, i, i + 1))
            continue
        if token == "ноль-ноль":
            close()
            found.append(Number(0, "00", i, i + 1))
            continue
        word = numeral(token)
        if word is None:
            close()
            continue
        value, kind, ordinal = word

        if group and group["kind"] == ZERO and kind in (ZERO, UNIT):
            # «ноль три» — месяц или минуты с ведущим нулём
            group.update(value=value, digits=f"0{value}", end=i + 1, ordinal=ordinal, kind=UNIT)
            close()
            continue
        if group and _extends(group, kind):
            if kind == THOUSAND and value == 1000:
                group["value"] *= 1000
            else:
                group["value"] += value
            group.update(kind=kind, end=i + 1, ordinal=ordinal)
        else:
            close()
            group = {"value": value, "kind": kind, "start": i, "end": i + 1, "ordinal": ordinal}
        if ordinal:
            close()
    close()
    return found


def _extends(group: dict, kind: int) -> bool:
    last = group["kind"]
    if group["ordinal"] or last == ZERO or kind == ZERO:
        return False
    if kind == THOUSAND:
        return last != THOUSAND and group["value"] < 1000
    if last in (UNIT, TEEN):
        return False
    return RANK[kind] < RANK[last]


def parse_digits(text: str, length: int = 16) -> str | None:
    """Номер, продиктованный цифрами или группами: полис ОМС — 16 цифр."""
    digits = "".join(n.digits for n in spoken_numbers(tokenize(text)))
    return digits if len(digits) == length else None
