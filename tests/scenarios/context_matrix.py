"""python -m tests.scenarios.context_matrix --output-dir ../.development/context [--max-gap 0.2]

Матрица контекста: эталонные диалоги T07 доводятся до каждого активного вопроса, и вместо
ожидаемой реплики подаётся пробная «не по сценарию» (context_probes.yaml). Каждый случай —
свежий прогон на эмуляторе МИС, память, без голоса. Это замер, а не приёмка кейса:
код 1 только при --max-gap, если доля пробелов выше порога; 2 — ошибка запуска.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from app.dialogue.models import Reply
from tests.scenarios.run import source_receipt
from tests.scenarios.runtime import ScenarioRuntime
from tests.scenarios.scenario_plan import HERE, Dialog, load_scenarios

PROBES = HERE / "context_probes.yaml"
OUTCOMES = ("answered", "transfer", "ended", "not_understood")


@dataclass
class Cell:
    base: str
    step: int
    phase: str
    probe: str
    text: str
    outcome: str
    verdict: str  # ok | gap | wrong
    code: str | None
    phase_after: str
    answer: str


def outcome(reply: Reply) -> str:
    said = " ".join(reply.utterances).casefold()
    if reply.transfer is not None:
        return "transfer"
    if reply.code == "NOT_UNDERSTOOD" or "не поняла" in said or "уточните, пожалуйста" in said:
        return "not_understood"
    if reply.phase == "ended":
        return "ended"
    return "answered"


def verdict(probe: dict, phase: str, result: str, code: str | None) -> str:
    if code in set(probe.get("forbidden_codes", ())):
        return "wrong"
    allowed = set(probe.get("allowed_in", {}).get(phase, probe["allowed"]))
    if result in allowed:
        return "ok"
    # «Не поняла» там, где нужен ответ, — пробел; другой исход — неверная реакция
    return "gap" if result == "not_understood" else "wrong"


def load_probes(path: Path = PROBES) -> dict:
    data = yaml.safe_load(path.read_text("utf-8"))
    for name, probe in data["probes"].items():
        if not probe.get("texts") or not set(probe["allowed"]) <= set(OUTCOMES):
            raise ValueError(f"неверное описание пробы {name}")
    return data


def patient_steps(dialog: Dialog) -> list[dict]:
    return [step for step in dialog.steps if "patient" in step]


async def run_case(dialog: Dialog, k: int, text: str) -> tuple[Reply | None, Reply] | None:
    runtime = ScenarioRuntime(dialog)
    replies: list[Reply] = []
    async with runtime.application() as client:
        if dialog.setup.get("channel") == "outbound":
            opening = await runtime.start_outbound()
            if opening is None:
                return None
            replies.append(opening)
        for step in patient_steps(dialog)[:k]:
            body = {"turn_id": len(replies) + 1, "text": step["patient"], "is_final": True}
            response = await client.post(f"/api/dialog/{runtime.call_id}/turn", json=body)
            replies.append(Reply.model_validate(response.json()))
        body = {"turn_id": len(replies) + 1, "text": text, "is_final": True}
        response = await client.post(f"/api/dialog/{runtime.call_id}/turn", json=body)
        return (replies[-1] if replies else None), Reply.model_validate(response.json())


async def execute(data: dict, bases=None, probes=None) -> list[Cell]:
    dialogs = {d.label: d for s in load_scenarios() for d in s.dialogs}
    cells = []
    for base in bases or data["bases"]:
        dialog = dialogs[base]
        # Входящий начинается с приветствия шлюза: первый активный вопрос — после реплики 1
        first = 0 if dialog.setup.get("channel") == "outbound" else 1
        for k in range(first, len(patient_steps(dialog))):
            for name, probe in data["probes"].items():
                if probes and name not in probes:
                    continue
                for text in probe["texts"]:
                    async with asyncio.timeout(10):
                        result = await run_case(dialog, k, text)
                    if result is None:
                        continue
                    before, after = result
                    phase = str(before.phase) if before else "greeting"
                    found = outcome(after)
                    cells.append(
                        Cell(
                            base=base,
                            step=k,
                            phase=phase,
                            probe=name,
                            text=text,
                            outcome=found,
                            verdict=verdict(probe, phase, found, after.code),
                            code=after.code,
                            phase_after=str(after.phase),
                            answer=" ".join(after.utterances)[:200],
                        )
                    )
    return cells


def summary(cells: list[Cell], data: dict) -> dict:
    def shares(group):
        counts = Counter(c.verdict for c in group)
        return {"n": len(group), **{k: counts.get(k, 0) for k in ("ok", "gap", "wrong")}}

    by_probe = defaultdict(list)
    by_state = defaultdict(list)
    for cell in cells:
        by_probe[cell.probe].append(cell)
        by_state[f"{cell.base} · {cell.phase}"].append(cell)
    total = shares(cells)
    return {
        "total": total,
        "gap_share": round(total["gap"] / total["n"], 3) if total["n"] else 0.0,
        "not_ok_share": round((total["gap"] + total["wrong"]) / total["n"], 3) if total["n"] else 0,
        "by_probe": {
            name: {**shares(group), "task": data["probes"][name].get("task")}
            for name, group in by_probe.items()
        },
        "by_state": {name: shares(group) for name, group in by_state.items()},
    }


def markdown(document: dict, data: dict) -> str:
    s = document["summary"]
    total = s["total"]
    lines = [
        "# Матрица контекста",
        "",
        f"Commit `{document['source']['commit']}`, dirty `{document['source']['dirty']}`. "
        "Эмулятор МИС, память, текстовый канал — без голоса и внешнего стенда.",
        "",
        f"Случаев **{total['n']}**: ok {total['ok']}, пробел («не поняла») {total['gap']}, "
        f"неверная реакция {total['wrong']}. Доля пробелов **{s['gap_share']:.0%}**, "
        f"не ok **{s['not_ok_share']:.0%}**.",
        "",
        "## По классам реплик",
        "",
        "| Класс | Задача | n | ok | пробел | неверно |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name, row in sorted(s["by_probe"].items(), key=lambda item: -item[1]["ok"] / item[1]["n"]):
        title = data["probes"][name]["title"]
        lines.append(
            f"| {title} | {row['task']} | {row['n']} | {row['ok']} | {row['gap']} | {row['wrong']} |"
        )
    lines += ["", "## По активным вопросам", "", "| Диалог · фаза | n | ok | пробел | неверно |"]
    lines.append("|---|---:|---:|---:|---:|")
    for name, row in s["by_state"].items():
        lines.append(f"| {name} | {row['n']} | {row['ok']} | {row['gap']} | {row['wrong']} |")
    wrong = [c for c in document["cells"] if c["verdict"] == "wrong"]
    if wrong:
        lines += [
            "",
            "## Неверные реакции",
            "",
            "| Диалог · фаза | Реплика | Исход | Код | Ответ |",
        ]
        lines.append("|---|---|---|---|---|")
        for c in wrong:
            answer = c["answer"].replace("|", "/")[:90]
            lines.append(
                f"| {c['base']} · {c['phase']} | {c['text']} | {c['outcome']} | {c['code']} | {answer} |"
            )
    return "\n".join(lines) + "\n"


def report(cells: list[Cell], data: dict) -> dict:
    return {
        "schema_version": 1,
        "source": source_receipt(),
        "scope": "HTTP ASGI / memory stores / isolated MIS emulator / text channel",
        "summary": summary(cells, data),
        "cells": [asdict(c) for c in cells],
    }


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-gap", type=float, help="порог доли «не поняла» для кода 1")
    parser.add_argument("--base", action="append", help="только эти диалоги (можно несколько)")
    parser.add_argument("--probe", action="append", help="только эти классы реплик")
    args = parser.parse_args(argv)
    data = load_probes()
    try:
        cells = asyncio.run(execute(data, args.base, args.probe))
    except Exception as error:
        print(f"матрица не построена: {type(error).__name__}")
        return 2
    document = report(cells, data)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "context.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (args.output_dir / "context.md").write_text(markdown(document, data), encoding="utf-8")
    s = document["summary"]
    print(json.dumps({"total": s["total"], "gap_share": s["gap_share"]}, ensure_ascii=False))
    if args.max_gap is not None and s["gap_share"] > args.max_gap:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
