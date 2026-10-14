"""speech.render: реплика по шаблону из config/phrases.yaml — текст для канала и разметка для синтеза.

Текст — слова так, как их произносит ассистент: «в пятницу, второго октября, в девять тридцать».
Его же видит текстовый канал, по нему считается лимит в 20 слов (раздел 3.2). Разметка для Silero
добавляет ударения из config/speech.yaml и интонационный центр общего вопроса: «*Верно*?».
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path

import yaml

from app.nlu.dictionaries import CONFIG_DIR, load_specialties
from app.nlu.lexicon import morph
from app.voice.numerals import CASES, agreement, cardinal, ordinal
from app.voice.phrases import MAX_WORDS, PLACEHOLDER, Phrasebook, count_words, load_phrasebook

SPEECH_PATH = CONFIG_DIR / "speech.yaml"

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_ACCS = ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье")
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
# Написания одного профиля, как их сводит backend при сопоставлении врачей
SPECIALTY_ALIASES = {
    "лор (отоларинголог)": "лор",
    "отоларинголог": "лор",
    "оториноларинголог": "лор",
    "детский отоларинголог": "детский лор",
    "детский оториноларинголог": "детский лор",
    "травматолог-ортопед": "травматолог",
}
# «Послезавтра» на слух путают с «завтра»: дальше завтрашнего дня — день недели, как в разделе 5
RELATIVE = {0: "сегодня", 1: "завтра"}
# Если реплика не помещается в 20 слов: «в среду, двадцать восьмого октября» → «двадцать восьмого октября»
COMPACT_DATES = {"": "short", "nomn": "short_nomn", "accs": "short_accs"}
PATIENT_ADDRESS = "{patient}, "
# «В пятницу» понятно без числа, только если до этого дня меньше недели
NEAR_DAYS = 6

# Общий вопрос без вопросительного слова: «Записать вас?» В специальном вопросе («Что удобнее?»)
# и альтернативном («Перенести или отменить?») модель ставит интонацию сама.
QUESTION_WORDS = {
    "кто",
    "что",
    "какой",
    "который",
    "чей",
    "когда",
    "где",
    "куда",
    "откуда",
    "сколько",
    "как",
    "почему",
    "зачем",
}
# «Хотите записаться?», «Могу ещё чем-то помочь?» — центр на глаголе после модального
MODALS = {"мочь", "хотеть", "хотеться", "можно", "нужно", "надо"}
PREDICATES = {"VERB", "INFN"}
SHORT_PREDICATES = {"PRED", "ADJS", "PRTS"}

WORD = re.compile(r"[А-Яа-яЁёA-Za-z]+(?:-[А-Яа-яЁёA-Za-z]+)*")
STRESS_ENDING = 3
SENTENCE = re.compile(r"[^.!?]*[.!?]+\s*|[^.!?]+$")
SENTENCE_START = re.compile(r"(^|[.!?]\s+)([*«\"]?)([а-яё])")
PLACEHOLDER_AND_WORD = re.compile(PLACEHOLDER.pattern + r"(?:(\s+)([а-яё]+))?")

DATE_TIME_DIGITS = re.compile(
    r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?:,?\s+(в\s+)?(\d{1,2}):(\d{2}))?(?![\d:])(?!\.\d)"
)
TIME_DIGITS = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])(?!\.\d)")
NUMBER_AND_WORD = re.compile(r"(?<![\d])(\d+)(?![\d])(?:(\s+)([а-яё]+))?")
GENITIVE_PREPOSITIONS = {"до", "с", "со", "от", "после", "около"}


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class Utterance:
    text: str
    speech: str
    template: str | None = None
    expect: str | None = None
    route: str | None = None
    end: bool = False

    @property
    def words(self) -> int:
        return count_words(self.text)


@dataclass(frozen=True)
class Pronunciation:
    intensity: int
    abbreviations: dict[str, str]
    stress: dict[str, str]


@lru_cache(maxsize=None)
def load_pronunciation(path: Path = SPEECH_PATH) -> Pronunciation:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    stress = {}
    for word, marked in (raw.get("stress") or {}).items():
        if marked.count("+") != 1 or marked.replace("+", "").lower() != str(word).lower():
            raise RenderError(f"ударение {marked!r}: нужен один «+» в слове {word!r}")
        stress[str(word).lower()] = marked
    return Pronunciation(
        intensity=int(raw["focus_intensity"]),
        abbreviations={str(k): str(v) for k, v in (raw.get("abbreviations") or {}).items()},
        stress=stress,
    )


def say_date(day: date, form: str = "", today: date | None = None) -> str:
    """Дата по формам из config/phrases.yaml: «в пятницу, второго октября», «пятницу», «завтра»."""
    if isinstance(day, datetime):
        day = day.date()
    ahead = (day - today).days if today else None
    relative = RELATIVE.get(ahead) if ahead is not None else None
    near = ahead is not None and 0 <= ahead <= NEAR_DAYS
    # Год называем, только если без него непонятно: дата прошла или до неё больше полугода
    far = ahead is not None and not 0 <= ahead <= 180
    year = f" {ordinal(day.year, 'gent')} года" if far and day.year != today.year else ""
    of_month = f"{ordinal(day.day, 'gent', 'neut')} {MONTHS[day.month - 1]}{year}"
    on_month = f"{ordinal(day.day, 'nomn', 'neut')} {MONTHS[day.month - 1]}{year}"
    weekday = day.weekday()
    on_weekday = f"{'во' if weekday == 1 else 'в'} {WEEKDAYS_ACCS[weekday]}"
    if form == "":
        return f"{relative or on_weekday}, {of_month}"
    if form == "nomn":
        return f"{relative or WEEKDAYS[weekday]}, {on_month}"
    if form == "accs":
        return f"{relative or WEEKDAYS_ACCS[weekday]}, {on_month}"
    if form == "gent":
        return of_month
    if form == "short":
        return relative or (on_weekday if near else of_month)
    if form == "short_accs":
        return relative or (WEEKDAYS_ACCS[weekday] if near else on_month)
    if form == "short_nomn":
        return relative or (WEEKDAYS[weekday] if near else on_month)
    raise RenderError(f"у даты нет формы {form!r}")


def say_time(at: time, form: str = "") -> str:
    """Время: «в девять тридцать», «двенадцать ноль-ноль», «с восьми до четырнадцати»."""
    if isinstance(at, datetime):
        at = at.time()
    case = "gent" if form == "gent" else "nomn"
    hour = cardinal(at.hour, case)
    if at.minute == 0:
        words = hour if case == "gent" else f"{hour} ноль-ноль"
    elif at.minute < 10:
        words = f"{hour} ноль {cardinal(at.minute, case, 'femn')}"
    else:
        words = f"{hour} {cardinal(at.minute, case, 'femn')}"
    if form == "":
        return f"в {words}"
    if form in ("nomn", "gent"):
        return words
    raise RenderError(f"у времени нет формы {form!r}")


def say_person(name: str, case: str = "nomn", *, surname_only: bool = False) -> str:
    """ФИО или его часть в падеже: «Кузнецова Анна Сергеевна» → datv «Кузнецовой Анне Сергеевне».

    Порядок как в справочнике: фамилия, имя, отчество; пациента называем по имени и отчеству.
    """
    words = name.split()
    roles = _roles(words)
    gender = _gender(words, roles)
    if surname_only and "Surn" in roles:
        words, roles = [words[roles.index("Surn")]], ["Surn"]
    if case == "nomn":
        return " ".join(words)
    return " ".join(_inflect_name(w, r, gender, case) for w, r in zip(words, roles))


def say_specialty(name: str, form: str = "") -> str:
    """Специальность по формам из config/synonyms.yaml; незнакомую склоняем по словам.

    Название может прийти со стенда («Лор (отоларинголог)») или из истории («оториноларинголог»).
    """
    form = form or "nomn"
    specialties = load_specialties()
    known = None
    for candidate in (name.lower(), SPECIALTY_ALIASES.get(name.lower().replace("ё", "е"))):
        known = specialties.get(candidate) or next(
            (s for s in specialties.values() if candidate in s.forms.values()), None
        )
        if known is not None:
            return known.forms[form]
    return _inflect_phrase(name, form)


def say_options(options: Sequence, today: date | None = None) -> str:
    """Варианты через запятую и «или». Врача и специальность называем, только если они различаются."""
    specialties = {o.specialty for o in options if getattr(o, "specialty", None)}
    doctors = {o.doctor for o in options if getattr(o, "doctor", None)}
    said = []
    previous_day = None
    for option in options:
        words = []
        if len(specialties) > 1 and option.specialty:
            words.append(f"к {say_specialty(option.specialty, 'datv')}")
        if len(doctors) > 1 and option.doctor:
            doctor = say_person(option.doctor, "datv", surname_only=True)
            words.append(doctor if words else f"к {doctor}")
        if option.day is not None and option.day != previous_day:
            words.append(say_date(option.day, "short", today))
        if option.at is not None:
            words.append(say_time(option.at))
        previous_day = option.day
        said.append(" ".join(words))
    if not said:
        raise RenderError("нет вариантов")
    return said[0] if len(said) == 1 else f"{', '.join(said[:-1])} или {said[-1]}"


def say_number(value: int | str, case: str = "nomn", gender: str = "masc") -> str:
    """Число словами; в номере кабинета буквы остаются: «214а» → «двести четырнадцать а»."""
    if isinstance(value, int):
        return cardinal(value, case, gender)
    spoken = re.sub(r"\d+", lambda m: f" {cardinal(int(m.group()), case, gender)} ", str(value))
    return " ".join(spoken.replace("-", " ").split())


def say_digits(text: str, today: date) -> str:
    """Свободная строка с цифрами — словами: «02.10.2026 15:15» → «пятница, второе октября, пятнадцать пятнадцать»."""

    def date_time(m: re.Match) -> str:
        day, month, year, at, hour, minute = m.groups()
        try:
            value = date(int(year) if year else today.year, int(month), int(day))
        except ValueError:
            return m.group()
        if not year and (today - value).days > 180:
            # «02.01» в декабре — это январь следующего года
            value = value.replace(year=today.year + 1)
        preposition = _word_before(text, m.start())
        form = (
            "accs"
            if preposition == "на"
            else "gent"
            if preposition in GENITIVE_PREPOSITIONS
            else "nomn"
        )
        said = say_date(value, form, today)
        if hour is None:
            return said
        return f"{said}, {at or ''}{say_time(time(int(hour), int(minute)), 'nomn')}"

    def clock(m: re.Match) -> str:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return m.group()
        gent = _word_before(text, m.start()) in GENITIVE_PREPOSITIONS
        return say_time(time(hour, minute), "gent" if gent else "nomn")

    def number(m: re.Match) -> str:
        value, space, noun = int(m.group(1)), m.group(2), m.group(3)
        if not noun:
            return say_number(value)
        gender, case = _noun_form(noun)
        # Падеж видно по существительному только после единицы: «через одну минуту»
        if not (value % 10 == 1 and value % 100 != 11):
            case = "nomn"
        return f"{say_number(value, case, gender)}{space}{noun}"

    text = re.sub(r"№\s*", "номер ", text)
    text = DATE_TIME_DIGITS.sub(date_time, text)
    text = TIME_DIGITS.sub(clock, text)
    return NUMBER_AND_WORD.sub(number, text)


def mark_focus(text: str) -> str:
    """Выделить интонационный центр в общих вопросах, где его ещё не выделили: «Записать вас?» → «*Записать* вас?»."""
    return "".join(_focus(s) for s in SENTENCE.findall(text))


def plain(text: str) -> str:
    return text.replace("*", "").replace("+", "")


class Renderer:
    def __init__(
        self, phrasebook: Phrasebook | None = None, pronunciation: Pronunciation | None = None
    ):
        self.phrasebook = phrasebook or load_phrasebook()
        self.pronunciation = pronunciation or load_pronunciation()

    def render(
        self, template_id: str, today: date, *, retry: bool = False, **values: object
    ) -> Utterance:
        template = self.phrasebook.get(template_id)
        source = template.retry if retry and template.retry else template.text
        options = values.get("options")
        if template.options and options is not None and len(options) > template.options:
            raise RenderError(
                f"{template_id}: вариантов {len(options)}, в шаблон входит {template.options}"
            )
        for variant, compact in _shorter(source):
            marked = self._fill(template_id, variant, values, today, compact)
            text = plain(marked)
            if count_words(text) <= MAX_WORDS:
                return Utterance(
                    text=text,
                    speech=self.speech(marked),
                    template=template_id,
                    expect=template.expect,
                    route=template.route,
                    end=template.end,
                )
        raise RenderError(f"{template_id}: {count_words(text)} слов, больше {MAX_WORDS}")

    def fitting_options(
        self, template_id: str, today: date, options: Sequence, **values: object
    ) -> int:
        """Сколько первых вариантов помещается в реплику в 20 слов; 0 — ни одного."""
        limit = min(len(options), self.phrasebook.get(template_id).options or len(options))
        for count in range(limit, 0, -1):
            try:
                self.render(template_id, today, options=options[:count], **values)
            except RenderError:
                continue
            return count
        return 0

    def speak(self, text: str, today: date) -> str:
        """Разметка для синтеза по готовой строке — для реплик, собранных не из шаблона."""
        return self.speech(say_digits(text, today))

    def speech(self, text: str) -> str:
        text = mark_focus(text)
        for short, spoken in self.pronunciation.abbreviations.items():
            text = re.sub(rf"(?<![\w+]){re.escape(short)}(?![\w+])", spoken, text)
        if self.pronunciation.stress:
            text = WORD.sub(self._stressed, text)
        return text

    def _stressed(self, m: re.Match) -> str:
        word = m.group()
        lowered = word.lower()
        # Основа «лебедев» подходит к «Лебедеву»: окончание не длиннее трёх букв
        stems = [s for s in self.pronunciation.stress if lowered.startswith(s)]
        stem = max(stems, key=len, default=None)
        around = m.string[max(m.start() - 1, 0) : m.end() + 1]
        if stem is None or len(lowered) - len(stem) > STRESS_ENDING or "+" in around:
            return word
        marked = self.pronunciation.stress[stem]
        at = marked.index("+")
        return word[:at] + "+" + word[at:]

    def _fill(
        self, template_id: str, source: str, values: Mapping, today: date, compact: bool = False
    ) -> str:
        def substitute(m: re.Match) -> str:
            name, form, space, word = m.group(1), m.group(2) or "", m.group(3), m.group(4)
            if name not in values or values[name] is None:
                raise RenderError(f"{template_id}: нет значения для {{{name}}}")
            value = values[name]
            kind = self.phrasebook.parts[name].type
            tail = f"{space}{word}" if word else ""
            if kind == "number" and word and isinstance(value, int):
                # «стоит пятьсот рублей», «в течение одной минуты»: число и слово после него согласуем
                noun, gender = _agree(word, value, form or "nomn")
                return f"{say_number(value, form or 'nomn', gender)}{space}{noun}"
            if kind == "date" and compact:
                form = COMPACT_DATES.get(form, form)
            return self._part(kind, name, form, value, today) + tail

        filled = PLACEHOLDER_AND_WORD.sub(substitute, source)
        return SENTENCE_START.sub(lambda m: m.group(1) + m.group(2) + m.group(3).upper(), filled)

    def _part(self, kind: str, name: str, form: str, value: object, today: date) -> str:
        if kind == "date":
            return say_date(value, form, today)
        if kind == "time":
            return say_time(value, form)
        if kind == "doctor":
            if form == "full":
                return say_person(str(value))
            return say_person(str(value), form or "nomn", surname_only=True)
        if kind == "number":
            return say_number(value, form or "nomn")
        if kind == "options":
            return say_options(value, today)
        if name == "specialty":
            return say_specialty(str(value), form)
        if name == "patient":
            return say_person(str(value), form or "nomn")
        return _inflect_phrase(str(value), form) if form else str(value)


@lru_cache(maxsize=1)
def default_renderer() -> Renderer:
    return Renderer()


def render(template_id: str, today: date, *, retry: bool = False, **values: object) -> Utterance:
    return default_renderer().render(template_id, today, retry=retry, **values)


def _shorter(source: str) -> list[tuple[str, bool]]:
    """Шаблон и его сокращения, если с подставленными значениями выходит больше 20 слов.

    Сначала убираем обращение по имени и отчеству, потом называем дату короче.
    """
    variants = [(source, False)]
    if source.startswith(PATIENT_ADDRESS):
        variants.append((source[len(PATIENT_ADDRESS) :], False))
    variants.append((variants[-1][0], True))
    return variants


def _focus(sentence: str) -> str:
    if not sentence.rstrip().endswith("?") or "*" in sentence:
        return sentence
    words = list(WORD.finditer(sentence))
    lowered = [w.group().lower() for w in words]
    if not words or "или" in lowered or any(_asks(w) for w in lowered):
        return sentence
    if "ли" in lowered[1:]:
        # «Удобно ли вам?» — центр на слове перед «ли»
        target = lowered.index("ли", 1) - 1
    else:
        target = _predicate(lowered)
    m = words[target]
    return f"{sentence[: m.start()]}*{m.group()}*{sentence[m.end() :]}"


def _asks(word: str) -> bool:
    return any(
        p.normal_form in QUESTION_WORDS
        and p.tag.POS in {"NPRO", "ADJF", "ADVB", "CONJ"}
        and "Abbr" not in p.tag  # «к» — не сокращение «как»
        and p.score >= 0.1
        for p in morph().parse(word)
    )


def _predicate(words: list[str]) -> int:
    for i, word in enumerate(words):
        parses = morph().parse(word)
        best = parses[0]
        if best.tag.POS in PREDICATES or (
            best.tag.POS in SHORT_PREDICATES and best.normal_form in MODALS
        ):
            if best.normal_form in MODALS:
                following = _first(words[i + 1 :], {"INFN"})
                if following is not None:
                    return i + 1 + following
            return i
    found = _first(words, SHORT_PREDICATES)
    return found if found is not None else len(words) - 1


def _first(words: list[str], kinds: set[str]) -> int | None:
    for i, word in enumerate(words):
        if any(p.tag.POS in kinds for p in morph().parse(word)):
            return i
    return None


def _roles(words: list[str]) -> list[str]:
    if len(words) >= 3:
        return ["Surn", "Name", "Patr"] + ["Surn"] * (len(words) - 3)
    if len(words) == 2:
        return ["Name", "Patr"] if _has_tag(words[1], "Patr") else ["Surn", "Name"]
    if words and _has_tag(words[0], "Name") and not _has_tag(words[0], "Surn"):
        return ["Name"]
    return ["Surn"] * len(words)


def _gender(words: list[str], roles: list[str]) -> str:
    for role in ("Patr", "Name"):
        if role in roles:
            for p in morph().parse(words[roles.index(role)]):
                if role in p.tag and p.tag.gender in ("masc", "femn"):
                    return p.tag.gender
    surname = words[roles.index("Surn")] if "Surn" in roles else words[0]
    female = ("ова", "ева", "ёва", "ина", "ына", "ая")
    return "femn" if surname.lower().endswith(female) else "masc"


def _has_tag(word: str, tag: str) -> bool:
    return any(tag in p.tag for p in morph().parse(word))


def _inflect_name(word: str, role: str, gender: str, case: str) -> str:
    if "-" in word:
        return "-".join(_inflect_name(part, role, gender, case) for part in word.split("-"))
    for p in morph().parse(word):
        if role in p.tag and "nomn" in p.tag and (gender in p.tag or "ms-f" in p.tag):
            inflected = p.inflect({case})
            if inflected is not None:
                return _same_case(word, inflected.word)
    # Не нашлась форма — оставляем как есть: несклоняемая фамилия лучше неверно склонённой
    return word


def _inflect_phrase(text: str, form: str) -> str:
    """Склонить начало словосочетания до главного существительного: «детский хирург» → «детскому хирургу»."""
    grammemes = {"nomn", "plur"} if form == "plur" else {form}
    words = text.split()
    for i, word in enumerate(words):
        parses = [p for p in morph().parse(word) if "nomn" in p.tag]
        if not parses:
            break
        inflected = parses[0].inflect(grammemes)
        if inflected is not None:
            words[i] = _same_case(word, inflected.word)
        if parses[0].tag.POS == "NOUN":
            break
    return " ".join(words)


def _agree(noun: str, number: int, case: str) -> tuple[str, str]:
    parses = [p for p in morph().parse(noun) if p.tag.POS == "NOUN"]
    if not parses:
        return noun, "masc"
    grammatical_number, noun_case = agreement(number, case)
    lemma = parses[0].normalized
    inflected = lemma.inflect({grammatical_number, noun_case})
    gender = lemma.tag.gender if lemma.tag.gender in ("masc", "femn", "neut") else "masc"
    return (_same_case(noun, inflected.word) if inflected else noun), gender


def _noun_form(word: str) -> tuple[str, str]:
    for p in morph().parse(word):
        if p.tag.POS == "NOUN" and p.tag.gender in ("masc", "femn", "neut"):
            return p.tag.gender, p.tag.case if p.tag.case in CASES else "nomn"
    return "masc", "nomn"


def _same_case(original: str, word: str) -> str:
    return word[0].upper() + word[1:] if original[:1].isupper() else word


def _word_before(text: str, position: int) -> str:
    before = WORD.findall(text[:position])
    return before[-1].lower() if before else ""
