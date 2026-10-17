import pytest

from app.voice.numerals import agreement, cardinal, ordinal


@pytest.mark.parametrize(
    "number, case, gender, words",
    [
        (0, "nomn", "masc", "ноль"),
        (1, "nomn", "femn", "одна"),
        (1, "accs", "femn", "одну"),
        (2, "nomn", "femn", "две"),
        (7, "gent", "masc", "семи"),
        (8, "ablt", "masc", "восемью"),
        (21, "gent", "femn", "двадцати одной"),
        (40, "gent", "masc", "сорока"),
        (90, "loct", "masc", "девяноста"),
        (100, "gent", "masc", "ста"),
        (103, "nomn", "masc", "сто три"),
        (214, "gent", "masc", "двухсот четырнадцати"),
        (214, "ablt", "masc", "двумястами четырнадцатью"),
        (580, "datv", "masc", "пятистам восьмидесяти"),
        (1000, "nomn", "masc", "тысяча"),
        (1500, "accs", "masc", "тысячу пятьсот"),
        (2200, "nomn", "masc", "две тысячи двести"),
        (5000, "gent", "masc", "пяти тысяч"),
        (21000, "nomn", "masc", "двадцать одна тысяча"),
    ],
)
def test_cardinal(number, case, gender, words):
    assert cardinal(number, case, gender) == words


@pytest.mark.parametrize(
    "number, case, gender, words",
    [
        (1, "gent", "neut", "первого"),
        (2, "nomn", "neut", "второе"),
        (3, "nomn", "neut", "третье"),
        (3, "gent", "neut", "третьего"),
        (22, "gent", "neut", "двадцать второго"),
        (31, "nomn", "neut", "тридцать первое"),
        (40, "nomn", "masc", "сороковой"),
        (2027, "gent", "masc", "две тысячи двадцать седьмого"),
        (2000, "gent", "masc", "двухтысячного"),
    ],
)
def test_ordinal(number, case, gender, words):
    assert ordinal(number, case, gender) == words


@pytest.mark.parametrize(
    "number, case, form",
    [
        (1, "nomn", ("sing", "nomn")),  # один рубль
        (2, "nomn", ("sing", "gent")),  # два рубля
        (5, "accs", ("plur", "gent")),  # пять рублей
        (11, "nomn", ("plur", "gent")),  # одиннадцать рублей
        (21, "gent", ("sing", "gent")),  # двадцати одной минуты
        (15, "gent", ("plur", "gent")),  # пятнадцати минут
    ],
)
def test_noun_after_number(number, case, form):
    assert agreement(number, case) == form


def test_out_of_range():
    with pytest.raises(ValueError):
        cardinal(1_000_000)
    with pytest.raises(ValueError):
        ordinal(150)
