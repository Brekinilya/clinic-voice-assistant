"""Шаблоны реплик из config/phrases.yaml: загрузка, подстановки и проверка длины."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.resource_paths import config_directory

PHRASES_PATH = config_directory() / "phrases.yaml"

MAX_WORDS = 20
MAX_OPTIONS = 3
EXPECTS = {"yes_no", "choice", "full_name", "birth_date", "date_time", "free"}

CASES = {"nomn", "gent", "datv", "accs", "ablt", "loct"}
FORMS = {
    "date": {"", "nomn", "accs", "gent", "short", "short_accs"},
    "time": {"", "nomn", "gent"},
    "doctor": CASES | {"", "full"},
    "text": CASES | {"", "plur"},
    "number": CASES | {""},
    "options": {""},
}

# Обычная длина озвученной даты и времени по форме: «во вторник, шестого октября», «в восемь сорок пять».
DATE_WORDS = {"": 4, "nomn": 3, "accs": 3, "gent": 2, "short": 2, "short_accs": 1}
TIME_WORDS = {"": 4, "nomn": 3, "gent": 3}
OPTION_WORDS = 5  # «во вторник в одиннадцать двадцать»

PLACEHOLDER = re.compile(r"\{(\w+)(?::(\w+))?\}")
ROUTE_CODE = re.compile(r"^R-(0[1-9]|1[0-7])$")


class PhrasesError(ValueError):
    pass


@dataclass(frozen=True)
class Part:
    name: str
    type: str
    words: int = 1


@dataclass(frozen=True)
class Template:
    id: str
    text: str
    src: str
    retry: str | None = None
    expect: str | None = None
    options: int = 0
    route: str | None = None
    end: bool = False

    def placeholders(self) -> list[tuple[str, str]]:
        return placeholders(self.text)


@dataclass(frozen=True)
class Phrasebook:
    parts: dict[str, Part]
    templates: dict[str, Template]

    def get(self, template_id: str) -> Template:
        return self.templates[template_id]

    def too_long(self) -> list[tuple[str, int]]:
        found = []
        for t in self.templates.values():
            for text in filter(None, (t.text, t.retry)):
                words = self.estimate_words(text, t.options)
                if words > MAX_WORDS:
                    found.append((t.id, words))
        return found

    def estimate_words(self, text: str, options: int = 0) -> int:
        plain = PLACEHOLDER.sub(" ", text)
        total = count_words(plain)
        for name, form in placeholders(text):
            total += self._part_words(name, form, options)
        return total

    def _part_words(self, name: str, form: str, options: int) -> int:
        part = self.parts[name]
        if part.type == "date":
            return DATE_WORDS[form]
        if part.type == "time":
            return TIME_WORDS[form]
        if part.type == "options":
            return options * OPTION_WORDS + 1
        if part.type == "doctor" and form == "full":
            return 3
        return part.words


def placeholders(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2) or "") for m in PLACEHOLDER.finditer(text)]


def count_words(text: str) -> int:
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


def load_phrasebook(path: Path = PHRASES_PATH) -> Phrasebook:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return parse_phrasebook(raw)


def parse_phrasebook(raw: dict) -> Phrasebook:
    parts = {}
    for name, data in raw["parts"].items():
        if data["type"] not in FORMS:
            raise PhrasesError(f"подстановка {name}: неизвестный тип {data['type']!r}")
        parts[name] = Part(name=name, type=data["type"], words=int(data.get("words", 1)))

    templates = {tid: _template(tid, data, parts) for tid, data in raw["templates"].items()}
    return Phrasebook(parts=parts, templates=templates)


def _template(tid: str, data: dict, parts: dict[str, Part]) -> Template:
    t = Template(
        id=tid,
        text=data["text"],
        src=str(data["src"]),
        retry=data.get("retry"),
        expect=data.get("expect"),
        options=int(data.get("options", 0)),
        route=data.get("route"),
        end=bool(data.get("end", False)),
    )
    if t.expect is not None and t.expect not in EXPECTS:
        raise PhrasesError(f"{tid}: неизвестный expect {t.expect!r}")
    if t.route is not None and not ROUTE_CODE.match(t.route):
        raise PhrasesError(f"{tid}: маршрут {t.route!r} не из R-01…R-17")
    if t.end and t.expect is not None:
        raise PhrasesError(f"{tid}: завершающая реплика не может ждать ответа")

    used = placeholders(t.text) + (placeholders(t.retry) if t.retry else [])
    for name, form in used:
        if name not in parts:
            raise PhrasesError(f"{tid}: неизвестная подстановка {{{name}}}")
        if form not in FORMS[parts[name].type]:
            raise PhrasesError(f"{tid}: у {{{name}}} нет формы {form!r}")

    has_options = any(name == "options" for name, _ in used)
    if has_options != bool(t.options):
        raise PhrasesError(f"{tid}: options у шаблона и {{options}} в тексте должны быть вместе")
    if t.options > MAX_OPTIONS:
        raise PhrasesError(f"{tid}: больше {MAX_OPTIONS} вариантов за раз")
    return t
