"""Сценарии из tests/scenarios: загрузка и таблица покрытия.

python -m tests.scenarios.scenario_plan          — напечатать таблицу покрытия
python -m tests.scenarios.scenario_plan --write  — обновить её в tests/scenarios/README.md
"""

import re
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import yaml

from app.nlu.catalog import load_catalog
from app.nlu.parsers import Option
from app.voice.phrases import load_phrasebook

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
FOLDERS = ("control", "main", "routes")
STAND_TIME = "%d.%m.%Y %H:%M"
CONTROL_IDS = [f"D-{n:02d}" for n in range(1, 17)]
BLOCKING = {"D-01", "D-02", "D-03", "D-04", "D-05"}  # помечены «!» в приложении Д

SCENARIO_TITLES = {
    "С-01": "Запись на приём",
    "С-02": "Запись по направлению",
    "С-03": "Перенос записи",
    "С-04": "Отмена записи",
    "С-05": "Проверка своей записи",
    "С-06": "Вызов врача на дом",
    "С-07": "Справка о графике",
    "С-08": "Готовность анализов",
    "С-09": "Платные услуги",
    "С-10": "Исходящее напоминание",
    "С-11": "Лист ожидания",
    "С-12": "Звонок за ребёнка или другого",
    "С-13": "Прикрепление",
    "С-14": "Экстренная ситуация",
    "С-15": "Жалоба",
}
ROUTE_TITLES = {
    "R-01": "Красный флаг А",
    "R-02": "Просит сотрудника",
    "R-03": "Жалоба, конфликт",
    "R-04": "Настаивает на медицинском вопросе",
    "R-05": "Не понят дважды",
    "R-06": "Сбой стенда",
    "R-07": "Не идентифицирован",
    "R-08": "Вне каталога",
    "R-09": "Острая зубная боль",
    "R-10": "Платное вне справочника",
    "R-11": "Красный флаг Б",
    "R-12": "Колл-центр Минздрава",
    "R-13": "Полис ОМС",
    "R-14": "Звонок от организации",
    "R-15": "Не справляется с диалогом",
    "R-16": "Запрос по персональным данным",
    "R-17": "Тишина, помехи",
}


@dataclass
class Dialog:
    scenario: str
    name: str
    setup: dict
    steps: list[dict]
    result: dict

    @property
    def now(self) -> datetime:
        return datetime.strptime(self.setup["now"], STAND_TIME)

    @property
    def operators_on_duty(self) -> bool:
        return self.setup.get("operators_on_duty", on_duty(self.now))

    @property
    def label(self) -> str:
        return f"{self.scenario} {self.name}".strip()


@dataclass
class Scenario:
    id: str
    title: str
    source: str
    blocking: bool
    expected: str
    covers: list[str]
    path: Path
    dialogs: list[Dialog]


def on_duty(moment: datetime) -> bool:
    """Часы операторов регистратуры (раздел 2.2): будни 7:00–20:00, суббота 8:30–15:00."""
    t, weekday = moment.time(), moment.isoweekday()
    if weekday <= 5:
        return time(7) <= t < time(20)
    if weekday == 6:
        return time(8, 30) <= t < time(15)
    return False


def load_fixtures() -> dict:
    with open(HERE / "fixtures.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_scenarios() -> list[Scenario]:
    scenarios = []
    for folder in FOLDERS:
        for path in sorted((HERE / folder).glob("*.yaml")):
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            base = raw.get("setup", {})
            parts = raw.get("dialogs") or [
                {"name": "", "steps": raw["steps"], "result": raw.get("result", {})}
            ]
            dialogs = [
                Dialog(
                    raw["id"],
                    d.get("name", ""),
                    {**base, **d.get("setup", {})},
                    d["steps"],
                    d.get("result", {}),
                )
                for d in parts
            ]
            scenarios.append(
                Scenario(
                    id=raw["id"],
                    title=raw["title"],
                    source=raw["source"],
                    blocking=bool(raw.get("blocking")),
                    expected=raw["expected"],
                    covers=[str(c) for c in raw.get("covers", [])],
                    path=path,
                    dialogs=dialogs,
                )
            )
    return scenarios


def to_option(raw: str | dict, fixtures: dict) -> Option:
    """«01.10.2026 08:45» — время приёма; {doctor, at} — существующая запись."""
    if isinstance(raw, str):
        moment = datetime.strptime(raw, STAND_TIME)
        return Option(day=moment.date(), at=moment.time())
    moment = datetime.strptime(raw["at"], STAND_TIME)
    doctor = fixtures["doctors"][raw["doctor"]]
    return Option(
        day=moment.date(),
        at=moment.time(),
        doctor=doctor["full_name"].split()[0],
        specialty=doctor["specialty"],
    )


def doctor_surnames(fixtures: dict) -> list[str]:
    return [d["full_name"].split()[0] for d in fixtures["doctors"].values()]


def codes(scenario: Scenario) -> set[str]:
    """Что покрывает сценарий: сценарии регламента, правила перевода, интенты."""
    templates = load_phrasebook().templates
    found = set(scenario.covers)
    for dialog in scenario.dialogs:
        for step in dialog.steps:
            says = step.get("say", [])
            for template_id in [says] if isinstance(says, str) else says:
                src = templates[template_id].src
                if src.startswith("С-"):
                    found.add(src)
                if templates[template_id].route:
                    found.add(templates[template_id].route)
            if "transfer" in step:
                found.add(step["transfer"]["route"])
            if step.get("safety", {}).get("rule"):
                found.add(step["safety"]["rule"])
            if re.match(r"^I-\d\d$", str(step.get("intent", ""))):
                found.add(step["intent"])
    return found


def coverage_markdown() -> str:
    scenarios = load_scenarios()
    by_id = {s.id: s for s in scenarios}
    covered = {s.id: codes(s) for s in scenarios}
    lines = [
        "### Контрольные диалоги приложения Д",
        "",
        "| № | Что проверяем | Блок. | Сценарии и правила | Диалогов | Шагов уже проверяется |",
        "|---|---|---|---|---|---|",
    ]
    for sid in CONTROL_IDS + [
        s.id for s in scenarios if s.id.startswith("D-") and s.id not in CONTROL_IDS
    ]:
        s = by_id[sid]
        refs = ", ".join(sorted(c for c in covered[sid] if not c.startswith("I-")))
        checked = sum(1 for d in s.dialogs for step in d.steps if "patient" in step)
        lines.append(
            f"| {sid} | {s.title} | {'!' if s.blocking else ''} | {refs} | {len(s.dialogs)} | {checked} |"
        )

    def table(title: str, items: dict[str, str]) -> list[str]:
        rows = ["", f"### {title}", "", "| Код | Что | Где проверяется |", "|---|---|---|"]
        for code, name in items.items():
            where = ", ".join(s.id for s in scenarios if code in covered[s.id]) or "**нет**"
            rows.append(f"| {code} | {name} | {where} |")
        return rows

    intents = {code: intent.title for code, intent in load_catalog().intents.items()}
    lines += table("Сценарии регламента", SCENARIO_TITLES)
    lines += table("Правила перевода", ROUTE_TITLES)
    lines += table("Интенты", intents)
    return "\n".join(lines) + "\n"


START, END = "<!-- coverage:start -->", "<!-- coverage:end -->"


def write_readme() -> None:
    text = README.read_text(encoding="utf-8")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    README.write_bytes(f"{head}{START}\n{coverage_markdown()}{END}{tail}".encode("utf-8"))


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_readme()
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(coverage_markdown())
