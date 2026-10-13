"""Каталог интентов и параметров из config/intents.yaml."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "intents.yaml"

MODES = {"auto", "semi", "operator", "emergency"}
SERVICE_INTENTS = ("yes", "no", "choice", "repeat", "maybe", "opt_out", "unknown")
SLOT_TYPES = {"specialty", "text", "date", "time", "enum", "weekday_list", "int", "visit_ref", "digits"}

INTENT_CODE = re.compile(r"^I-\d{2}$")
SCENARIO_CODE = re.compile(r"^С-\d{2}$")  # кириллическая «С», как в регламенте
ROUTE_CODE = re.compile(r"^R-\d{2}$")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Intent:
    code: str
    title: str
    mode: str
    stage: int
    identify: bool
    scenarios: tuple[str, ...]
    slots: tuple[str, ...]
    required: tuple[str, ...]
    examples: tuple[str, ...]
    route: str | None = None
    description: str = ""


@dataclass(frozen=True)
class ServiceIntent:
    code: str
    title: str
    examples: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class Slot:
    name: str
    title: str
    type: str
    stage: int
    values: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Catalog:
    intents: dict[str, Intent]
    service_intents: dict[str, ServiceIntent]
    slots: dict[str, Slot]
    slot_examples: tuple[dict, ...]
    choice_examples: tuple[dict, ...]
    reference_date: str

    def all_intent_codes(self) -> tuple[str, ...]:
        return tuple(self.intents) + tuple(self.service_intents)


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return parse_catalog(raw)


def parse_catalog(raw: dict) -> Catalog:
    slots = {name: _slot(name, data) for name, data in raw["slots"].items()}
    intents = {code: _intent(code, data, slots) for code, data in raw["intents"].items()}
    service = {code: _service(code, data) for code, data in raw["service_intents"].items()}

    missing = set(SERVICE_INTENTS) - set(service)
    extra = set(service) - set(SERVICE_INTENTS)
    if missing or extra:
        raise CatalogError(f"служебные интенты не совпадают: нет {sorted(missing)}, лишние {sorted(extra)}")

    examples = raw.get("slot_examples", {})
    cases = tuple(examples.get("cases", ()))
    for case in cases:
        unknown = set(case["slots"]) - set(slots)
        if unknown:
            raise CatalogError(f"пример «{case['say']}»: неизвестные параметры {sorted(unknown)}")

    return Catalog(
        intents=intents,
        service_intents=service,
        slots=slots,
        slot_examples=cases,
        choice_examples=tuple(raw.get("choice_examples", ())),
        reference_date=str(examples.get("reference_date", "")),
    )


def _intent(code: str, data: dict, slots: dict[str, Slot]) -> Intent:
    if not INTENT_CODE.match(code):
        raise CatalogError(f"{code}: код интента должен быть вида I-01")
    if data["mode"] not in MODES:
        raise CatalogError(f"{code}: неизвестный режим {data['mode']!r}")

    route = data.get("route")
    if route is not None and not ROUTE_CODE.match(route):
        raise CatalogError(f"{code}: маршрут {route!r} должен быть вида R-01")
    if data["mode"] in {"operator", "emergency"} and route is None:
        raise CatalogError(f"{code}: для режима {data['mode']} нужен route")

    for scenario in data["scenarios"]:
        if not SCENARIO_CODE.match(scenario):
            raise CatalogError(f"{code}: сценарий {scenario!r} должен быть вида С-01 с кириллической С")

    intent_slots = tuple(data["slots"])
    unknown = set(intent_slots) - set(slots)
    if unknown:
        raise CatalogError(f"{code}: неизвестные параметры {sorted(unknown)}")
    required = tuple(data["required"])
    if not set(required) <= set(intent_slots):
        raise CatalogError(f"{code}: required должен входить в slots")

    examples = tuple(data["examples"])
    if not examples:
        raise CatalogError(f"{code}: нет примеров фраз")

    return Intent(
        code=code,
        title=data["title"],
        mode=data["mode"],
        stage=int(data["stage"]),
        identify=bool(data["identify"]),
        scenarios=tuple(data["scenarios"]),
        slots=intent_slots,
        required=required,
        examples=examples,
        route=route,
        description=data.get("description", ""),
    )


def _service(code: str, data: dict) -> ServiceIntent:
    examples = tuple(str(e) for e in data["examples"])
    if not examples:
        raise CatalogError(f"{code}: нет примеров фраз")
    return ServiceIntent(code=code, title=data["title"], examples=examples, description=data.get("description", ""))


def _slot(name: str, data: dict) -> Slot:
    if data["type"] not in SLOT_TYPES:
        raise CatalogError(f"{name}: неизвестный тип {data['type']!r}")
    values = tuple(data.get("values", ()))
    if data["type"] == "enum" and not values:
        raise CatalogError(f"{name}: для enum нужен список values")
    return Slot(
        name=name,
        title=data["title"],
        type=data["type"],
        stage=int(data["stage"]),
        values=values,
        note=data.get("note", ""),
    )
