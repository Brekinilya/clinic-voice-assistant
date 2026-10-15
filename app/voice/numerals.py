"""Числа словами для озвучивания: количественные во всех падежах и порядковые для дат."""

CASES = ("nomn", "gent", "datv", "accs", "ablt", "loct")

# Формы по падежам в порядке CASES; винительный — как у неодушевлённых («вижу два кабинета»)
_ONE = {
    "masc": ("один", "одного", "одному", "один", "одним", "одном"),
    "femn": ("одна", "одной", "одной", "одну", "одной", "одной"),
    "neut": ("одно", "одного", "одному", "одно", "одним", "одном"),
}
_TWO = {
    "masc": ("два", "двух", "двум", "два", "двумя", "двух"),
    "femn": ("две", "двух", "двум", "две", "двумя", "двух"),
    "neut": ("два", "двух", "двум", "два", "двумя", "двух"),
}
_SMALL = {
    0: ("ноль", "ноля", "нолю", "ноль", "нолём", "ноле"),
    3: ("три", "трёх", "трём", "три", "тремя", "трёх"),
    4: ("четыре", "четырёх", "четырём", "четыре", "четырьмя", "четырёх"),
    40: ("сорок", "сорока", "сорока", "сорок", "сорока", "сорока"),
    90: ("девяносто", "девяноста", "девяноста", "девяносто", "девяноста", "девяноста"),
    100: ("сто", "ста", "ста", "сто", "ста", "ста"),
}
# Числа на -ь склоняются как существительные третьего склонения: пять, пяти, пятью
_SOFT = {
    5: "пят",
    6: "шест",
    7: "сем",
    8: "восьм",
    9: "девят",
    10: "десят",
    11: "одиннадцат",
    12: "двенадцат",
    13: "тринадцат",
    14: "четырнадцат",
    15: "пятнадцат",
    16: "шестнадцат",
    17: "семнадцат",
    18: "восемнадцат",
    19: "девятнадцат",
    20: "двадцат",
    30: "тридцат",
}
_COMPOUND_TENS = {
    50: ("пят", "пятью"),
    60: ("шест", "шестью"),
    70: ("сем", "семью"),
    80: ("восьм", "восемью"),
}
_HUNDREDS = {
    200: ("двести", "двухсот", "двумстам", "двести", "двумястами", "двухстах"),
    300: ("триста", "трёхсот", "трёмстам", "триста", "тремястами", "трёхстах"),
    400: ("четыреста", "четырёхсот", "четырёмстам", "четыреста", "четырьмястами", "четырёхстах"),
}
_HUNDRED_STEMS = {500: "пят", 600: "шест", 700: "сем", 800: "восьм", 900: "девят"}
_THOUSAND = {
    "sing": ("тысяча", "тысячи", "тысяче", "тысячу", "тысячей", "тысяче"),
    "plur": ("тысячи", "тысяч", "тысячам", "тысячи", "тысячами", "тысячах"),
}

_ORDINAL_STEMS = {
    1: "перв",
    2: "втор",
    4: "четвёрт",
    5: "пят",
    6: "шест",
    7: "седьм",
    8: "восьм",
    9: "девят",
    10: "десят",
    11: "одиннадцат",
    12: "двенадцат",
    13: "тринадцат",
    14: "четырнадцат",
    15: "пятнадцат",
    16: "шестнадцат",
    17: "семнадцат",
    18: "восемнадцат",
    19: "девятнадцат",
    20: "двадцат",
    30: "тридцат",
    40: "сороков",
    50: "пятидесят",
    60: "шестидесят",
    70: "семидесят",
    80: "восьмидесят",
    90: "девяност",
}
# Ударное окончание: второй, шестой, седьмой, восьмой, сороковой
_STRESSED_END = {2, 6, 7, 8, 40}
_ORDINAL_ENDINGS = {
    "masc": ("ый", "ого", "ому", "ый", "ым", "ом"),
    "neut": ("ое", "ого", "ому", "ое", "ым", "ом"),
    "femn": ("ая", "ой", "ой", "ую", "ой", "ой"),
}
_THIRD = {
    "masc": ("третий", "третьего", "третьему", "третий", "третьим", "третьем"),
    "neut": ("третье", "третьего", "третьему", "третье", "третьим", "третьем"),
    "femn": ("третья", "третьей", "третьей", "третью", "третьей", "третьей"),
}


def cardinal(number: int, case: str = "nomn", gender: str = "masc") -> str:
    """Количественное число словами: cardinal(21, "gent", "femn") — «двадцати одной»."""
    if not 0 <= number < 1_000_000:
        raise ValueError(f"число {number} вне диапазона озвучивания")
    i = CASES.index(case)
    if number == 0:
        return _SMALL[0][i]
    words = []
    thousands, rest = divmod(number, 1000)
    if thousands:
        # «тысяча пятьсот», а не «одна тысяча пятьсот»
        if thousands != 1:
            words.append(_below_thousand(thousands, i, "femn"))
        words.append(_THOUSAND[_noun_number(thousands, case)][_noun_case(thousands, case)])
    if rest:
        words.append(_below_thousand(rest, i, gender))
    return " ".join(words)


def ordinal(number: int, case: str = "nomn", gender: str = "masc") -> str:
    """Порядковое число: ordinal(22, "gent", "neut") — «двадцать второго», ordinal(2027, "gent") — год."""
    i = CASES.index(case)
    if number == 2000:
        return "двухтысячн" + _ORDINAL_ENDINGS[gender][i]
    if 2000 < number < 2100:
        return "две тысячи " + ordinal(number - 2000, case, gender)
    if not 1 <= number < 100:
        raise ValueError(f"порядковое число {number} вне диапазона озвучивания")
    tens, units = divmod(number, 10)
    if number < 20 or units == 0:
        return _ordinal_word(number, i, gender)
    return f"{_below_thousand(tens * 10, 0, 'masc')} {_ordinal_word(units, i, gender)}"


def agreement(number: int, case: str) -> tuple[str, str]:
    """Число и падеж существительного после числа: 1 рубль, 2 рубля, 5 рублей; в течение 2 минут."""
    return _noun_number(number, case), CASES[_noun_case(number, case)]


def _below_thousand(number: int, i: int, gender: str) -> str:
    words = []
    hundreds, rest = divmod(number, 100)
    if hundreds:
        words.append(_hundreds(hundreds * 100, i))
    tens, units = divmod(rest, 10)
    if rest >= 20:
        words.append(_word(tens * 10, i, gender))
        if units:
            words.append(_word(units, i, gender))
    elif rest:
        words.append(_word(rest, i, gender))
    return " ".join(words)


def _word(number: int, i: int, gender: str) -> str:
    if number == 1:
        return _ONE[gender][i]
    if number == 2:
        return _TWO[gender][i]
    if number in _SMALL:
        return _SMALL[number][i]
    if number in _SOFT:
        stem = _SOFT[number]
        nomn = {5: "пять", 6: "шесть", 7: "семь", 8: "восемь"}.get(number, stem + "ь")
        if i in (0, 3):
            return nomn
        if i == 4:
            return "восемью" if number == 8 else stem + "ью"
        return stem + "и"
    if number in _COMPOUND_TENS:
        stem, ablt = _COMPOUND_TENS[number]
        nomn = {50: "пятьдесят", 60: "шестьдесят", 70: "семьдесят", 80: "восемьдесят"}[number]
        if i in (0, 3):
            return nomn
        if i == 4:
            return ablt + "десятью"
        return stem + "идесяти"
    raise ValueError(f"нет слова для {number}")


def _hundreds(number: int, i: int) -> str:
    if number == 100:
        return _SMALL[100][i]
    if number in _HUNDREDS:
        return _HUNDREDS[number][i]
    stem = _HUNDRED_STEMS[number]
    nomn = {500: "пятьсот", 600: "шестьсот", 700: "семьсот", 800: "восемьсот"}.get(
        number, "девятьсот"
    )
    ablt = {500: "пятью", 600: "шестью", 700: "семью", 800: "восемью", 900: "девятью"}[number]
    return (nomn, stem + "исот", stem + "истам", nomn, ablt + "стами", stem + "истах")[i]


def _noun_number(number: int, case: str) -> str:
    last, last_two = number % 10, number % 100
    if last == 1 and last_two != 11:
        return "sing"
    if case in ("nomn", "accs") and 2 <= last <= 4 and not 12 <= last_two <= 14:
        return "sing"
    return "plur"


def _noun_case(number: int, case: str) -> int:
    # После 2–4 и от 5 в именительном и винительном существительное стоит в родительном
    last, last_two = number % 10, number % 100
    if case in ("nomn", "accs") and not (last == 1 and last_two != 11):
        return CASES.index("gent")
    return CASES.index(case)


def _ordinal_word(number: int, i: int, gender: str) -> str:
    if number == 3:
        return _THIRD[gender][i]
    stem = _ORDINAL_STEMS[number]
    ending = _ORDINAL_ENDINGS[gender][i]
    if number in _STRESSED_END and ending == "ый":
        ending = "ой"
    return stem + ending
