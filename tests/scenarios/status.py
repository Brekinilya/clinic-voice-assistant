"""python -m tests.scenarios.status --output-dir ../.development/status [--previous прошлый/status.json]

Одна сводка состояния для всей команды: строгая приёмка T07 по задачам, которые её
блокируют, и матрица контекста по классам реплик. С --previous показывает, что изменилось.
Источник правды — этот отчёт на конкретном commit, а не память агента и прошлые документы.
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tests.scenarios import context_matrix
from tests.scenarios.run import execute_suite, source_receipt

TASK = re.compile(r"_(T\d\d(?:_T\d\d)?)$")


def blockers(result: dict) -> set[str]:
    """Задача плана, от которой зависит сценарий; без пометки — сам диалог."""
    tags = {m.group(1) for f in result["findings"] if (m := TASK.search(f["code"]))}
    return tags or {"диалог"}


def t07_section(results: list) -> dict:
    rows = [r.serializable() for r in results]
    by_task = defaultdict(list)
    for row in rows:
        if row["status"] != "pass":
            for tag in blockers(row):
                by_task[tag].append(row["label"])
    return {
        "counts": dict(Counter(row["status"] for row in rows)),
        "total": len(rows),
        "passed": sorted(row["label"] for row in rows if row["status"] == "pass"),
        "by_task": {k: sorted(v) for k, v in sorted(by_task.items(), key=lambda i: -len(i[1]))},
        "statuses": {row["label"]: row["status"] for row in rows},
    }


def changes(current: dict, previous: dict | None) -> dict:
    if previous is None:
        return {}
    before, after = previous["t07"]["statuses"], current["t07"]["statuses"]
    gained = sorted(k for k, v in after.items() if v == "pass" and before.get(k) != "pass")
    lost = sorted(k for k, v in before.items() if v == "pass" and after.get(k) != "pass")
    probes_before = previous["context"]["by_probe"]
    probes = {
        name: row["ok"] - probes_before.get(name, {}).get("ok", 0)
        for name, row in current["context"]["by_probe"].items()
        if row["ok"] != probes_before.get(name, {}).get("ok", 0)
    }
    return {
        "previous_commit": previous["source"]["commit"],
        "t07_gained": gained,
        "t07_lost": lost,
        "context_gap_share": [previous["context"]["gap_share"], current["context"]["gap_share"]],
        "context_ok_delta": probes,
    }


def markdown(document: dict, probes: dict) -> str:
    t07, context, delta = document["t07"], document["context"], document["changes"]
    counts = t07["counts"]
    lines = [
        "# Состояние проекта",
        "",
        f"Commit `{document['source']['commit']}`, dirty `{document['source']['dirty']}`, "
        f"проверено {document['checked_at'][:16]} UTC. Эмулятор МИС, память, текстовый канал.",
        "",
        f"**T07:** pass {counts.get('pass', 0)} / fail {counts.get('fail', 0)} / "
        f"unsupported {counts.get('unsupported', 0)} из {t07['total']}. "
        f"**Контекст:** «не поняла» {context['gap_share']:.0%}, "
        f"не ok {context['not_ok_share']:.0%} из {context['total']['n']} случаев.",
    ]
    if delta:
        lines += [
            "",
            f"## Изменения с `{delta['previous_commit'][:7]}`",
            "",
            f"- T07 стали pass: {', '.join(delta['t07_gained']) or '—'}",
            f"- T07 перестали проходить: {', '.join(delta['t07_lost']) or '—'}",
            "- «Не поняла»: {:.0%} → {:.0%}".format(*delta["context_gap_share"]),
        ]
        for name, value in delta["context_ok_delta"].items():
            lines.append(f"- {probes['probes'][name]['title']}: ok {value:+d}")
    lines += ["", "## T07: что держит непройденные сценарии", "", "| Задача | n | Сценарии |"]
    lines.append("|---|---:|---|")
    for task, labels in t07["by_task"].items():
        lines.append(f"| {task} | {len(labels)} | {'; '.join(labels)} |")
    lines += ["", "## Контекст: классы реплик", "", "| Класс | Задача | ok из n | «не поняла» |"]
    lines.append("|---|---|---:|---:|")
    for name, row in sorted(context["by_probe"].items(), key=lambda i: i[1]["ok"] / i[1]["n"]):
        title = probes["probes"][name]["title"]
        lines.append(f"| {title} | {row['task']} | {row['ok']}/{row['n']} | {row['gap']} |")
    lines += [
        "",
        "Подробности: `python -m tests.scenarios.run --output-dir …` и "
        "`python -m tests.scenarios.context_matrix --output-dir …`.",
    ]
    return "\n".join(lines) + "\n"


async def collect():
    results = await execute_suite()
    probes = context_matrix.load_probes()
    cells = await context_matrix.execute(probes)
    return results, probes, cells


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous", type=Path, help="status.json прошлого прогона")
    args = parser.parse_args(argv)
    results, probes, cells = asyncio.run(collect())
    document = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": source_receipt(),
        "t07": t07_section(results),
        "context": context_matrix.summary(cells, probes),
    }
    previous = None
    if args.previous and args.previous.is_file():
        previous = json.loads(args.previous.read_text("utf-8"))
    document["changes"] = changes(document, previous)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "status.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.output_dir / "STATUS.md").write_text(markdown(document, probes), encoding="utf-8")
    # Консоль Windows в cp1251 не печатает стрелки и кавычки отчёта
    sys.stdout.reconfigure(encoding="utf-8")
    print((args.output_dir / "STATUS.md").read_text("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
