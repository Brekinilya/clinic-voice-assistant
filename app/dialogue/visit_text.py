"""Произношение параметров посещения: день недели, дата и время словами в часовом поясе МИС."""

import re
from datetime import datetime

from app.clock import CLINIC_TIMEZONE

WEEKDAYS = (
    "в понедельник",
    "во вторник",
    "в среду",
    "в четверг",
    "в пятницу",
    "в субботу",
    "в воскресенье",
)
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
DAYS = (
    "первого",
    "второго",
    "третьего",
    "четвёртого",
    "пятого",
    "шестого",
    "седьмого",
    "восьмого",
    "девятого",
    "десятого",
    "одиннадцатого",
    "двенадцатого",
    "тринадцатого",
    "четырнадцатого",
    "пятнадцатого",
    "шестнадцатого",
    "семнадцатого",
    "восемнадцатого",
    "девятнадцатого",
    "двадцатого",
    "двадцать первого",
    "двадцать второго",
    "двадцать третьего",
    "двадцать четвёртого",
    "двадцать пятого",
    "двадцать шестого",
    "двадцать седьмого",
    "двадцать восьмого",
    "двадцать девятого",
    "тридцатого",
    "тридцать первого",
)
UNITS = (
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
TENS = (
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
HUNDREDS = (
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
)


def number_words(number: int) -> str:
    if not 0 <= number <= 9999:
        raise ValueError("число вне диапазона произношения")
    if number < 20:
        return UNITS[number]
    words = []
    thousands, rest = divmod(number, 1000)
    if thousands:
        words.append(
            "одна тысяча"
            if thousands == 1
            else "две тысячи"
            if thousands == 2
            else f"{UNITS[thousands]} {'тысячи' if thousands < 5 else 'тысяч'}"
        )
    hundreds, rest = divmod(rest, 100)
    if hundreds:
        words.append(HUNDREDS[hundreds])
    if rest:
        if rest < 20:
            words.append(UNITS[rest])
        else:
            tens, units = divmod(rest, 10)
            words.append(TENS[tens])
            if units:
                words.append(UNITS[units])
    return " ".join(words)


def appointment_text(moment: datetime) -> tuple[str, str]:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("у посещения должен быть часовой пояс")
    moment = moment.astimezone(CLINIC_TIMEZONE)
    minutes = (
        "ровно"
        if moment.minute == 0
        else (
            "ноль " + number_words(moment.minute)
            if moment.minute < 10
            else number_words(moment.minute)
        )
    )
    return (
        f"Приём {WEEKDAYS[moment.weekday()]}, {DAYS[moment.day - 1]} {MONTHS[moment.month - 1]}, "
        f"в {number_words(moment.hour)} {minutes}.",
        f"Год приёма: {number_words(moment.year)}.",
    )


def room_text(room: str | None) -> str:
    if room is None:
        return "Номер кабинета в доступном справочнике отсутствует."
    spoken = number_words(int(room)) if re.fullmatch(r"[0-9]{1,4}", room) else room
    return f"Кабинет {spoken}."
