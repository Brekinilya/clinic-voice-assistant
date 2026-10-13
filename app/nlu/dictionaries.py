"""Загрузка словарей из config/: красные флаги, сигналы для safety.check, синонимы приложения А."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.nlu.lexicon import Entry, Lexicon, LexiconError

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
RED_FLAGS_PATH = CONFIG_DIR / "red_flags.yaml"
FLAGS_PATH = CONFIG_DIR / "flags.yaml"
SYNONYMS_PATH = CONFIG_DIR / "synonyms.yaml"

RED_CATEGORIES = ("red_a", "red_b")
FORM_KEYS = ("nomn", "gent", "datv", "accs", "ablt", "loct", "plur")
AGES = {"adult", "child", "any"}


@dataclass(frozen=True)
class Specialty:
    name: str
    referral: bool
    age: str
    forms: dict[str, str]


def read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def load_red_flags(path: Path = RED_FLAGS_PATH) -> Lexicon:
    """Ключи записей — «red_a.heart», «red_b.fever» и т. п."""
    raw = read_yaml(path)
    entries = [
        Entry.from_dict(f"{category}.{group}", data)
        for category in RED_CATEGORIES
        for group, data in raw[category]["groups"].items()
    ]
    for entry in entries:
        if entry.exceptions:
            raise LexiconError(f"{entry.key}: у красных флагов не бывает исключений")
    return Lexicon(entries, raw["window"])


@lru_cache(maxsize=None)
def load_flags(path: Path = FLAGS_PATH) -> Lexicon:
    raw = read_yaml(path)
    return Lexicon([Entry.from_dict(k, v) for k, v in raw["flags"].items()], raw["window"])


@lru_cache(maxsize=None)
def load_specialties(path: Path = SYNONYMS_PATH) -> dict[str, Specialty]:
    specialties = {}
    for name, data in read_yaml(path)["specialties"].items():
        forms = data["forms"]
        if len(forms) != len(FORM_KEYS):
            raise LexiconError(f"{name}: нужно {len(FORM_KEYS)} форм ({', '.join(FORM_KEYS)})")
        if data["age"] not in AGES:
            raise LexiconError(f"{name}: возраст {data['age']!r} не из {sorted(AGES)}")
        specialties[name] = Specialty(name, bool(data["referral"]), data["age"], dict(zip(FORM_KEYS, forms)))
    return specialties


@lru_cache(maxsize=None)
def specialty_lexicon(path: Path = SYNONYMS_PATH) -> Lexicon:
    return _section_lexicon(path, "specialties")


@lru_cache(maxsize=None)
def service_lexicon(path: Path = SYNONYMS_PATH) -> Lexicon:
    return _section_lexicon(path, "services")


@lru_cache(maxsize=None)
def action_lexicon(path: Path = SYNONYMS_PATH) -> Lexicon:
    return _section_lexicon(path, "actions")


@lru_cache(maxsize=None)
def day_part_lexicon(path: Path = SYNONYMS_PATH) -> Lexicon:
    return _section_lexicon(path, "day_parts")


def _section_lexicon(path: Path, section: str) -> Lexicon:
    raw = read_yaml(path)
    return Lexicon([Entry.from_dict(k, v) for k, v in raw[section].items()], raw["window"])
