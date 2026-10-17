"""python -m tests.scenarios.run --output-dir ../.development/t07 --require all

Код 1 — приёмка не пройдена; 2 — ошибка запуска/неполный набор. Отчёт пишется
до проверки порога. Нельзя использовать зелёный pytest вместо этого порога.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tests.scenarios.regressions import REGRESSION_LABEL, quota_regression
from tests.scenarios.runner import DialogResult, run_dialog
from tests.scenarios.scenario_plan import CONTROL_IDS, HERE, load_scenarios

ROOT = HERE.parents[1]


def source_receipt():
    digest = hashlib.sha256()
    for folder in (ROOT / "app", ROOT / "config", HERE):
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".yaml", ".json", ".md"}:
                digest.update(
                    path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes()
                )
    for path in (
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / ".github/workflows/scenario-acceptance.yml",
    ):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())

    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "source_sha256": digest.hexdigest(),
    }


async def execute_suite():
    results = []
    for scenario in load_scenarios():
        for dialog in scenario.dialogs:
            try:
                async with asyncio.timeout(10):
                    result = await run_dialog(dialog, blocking=scenario.blocking)
            except TimeoutError:
                result = DialogResult(dialog.scenario, dialog.label, scenario.blocking)
                result.add("runner_timeout", "Диалог не завершился за 10 секунд", "error")
                result.finish()
            except Exception as error:
                result = DialogResult(dialog.scenario, dialog.label, scenario.blocking)
                result.add("fixture_or_runner_error", type(error).__name__, "error")
                result.finish()
            results.append(result)
            print(f"{dialog.label}: {result.status}", flush=True)
    try:
        dialog, runtime = quota_regression()
        async with asyncio.timeout(10):
            results.append(await run_dialog(dialog, runtime=runtime))
    except Exception as error:
        result = DialogResult("REG-T05", REGRESSION_LABEL, False)
        result.add("fixture_or_runner_error", type(error).__name__, "error")
        results.append(result.finish())
    return results


def accepted(results, requirement):
    ids = {r.scenario_id for r in results}
    if not set(CONTROL_IDS) <= ids:
        return False
    expected = {d.label for s in load_scenarios() for d in s.dialogs} | {REGRESSION_LABEL}
    if expected != {r.label for r in results} or len(results) != len(expected):
        return False
    relevant = [r for r in results if requirement == "all" or r.blocking]
    return bool(relevant) and all(r.status == "pass" for r in relevant)


def report(results):
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": source_receipt(),
        "runtime": {"python": platform.python_version(), "system": platform.system()},
        "scope": "HTTP ASGI / memory stores / isolated MIS emulator",
        "not_verified": ["SIP", "ASR/TTS", "external MIS", "PostgreSQL restart/multiprocess"],
        "counts": dict(Counter(r.status for r in results)),
        "blocking_accepted": accepted(results, "blocking"),
        "all_accepted": accepted(results, "all"),
        "blocking_ids": sorted({r.scenario_id for r in results if r.blocking}),
        "results": [r.serializable() for r in results],
        "request_scenarios": [
            r.serializable()
            for r in results
            if r.scenario_id in {"M-01", "M-03", "M-04", "M-05", "M-10", "REG-T05"}
        ],
    }


def markdown(document):
    lines = [
        "# Приёмка текстовых сценариев",
        "",
        f"Commit: `{document['source']['commit']}`; dirty: `{document['source']['dirty']}`.",
        f"Хеш проверенных исходников: `{document['source']['source_sha256']}`.",
        "",
        "**Это локальный HTTP-прогон с эмулятором, не голос и не внешний стенд.**",
        "",
        f"Итоги: {document['counts']}. Блокирующие приняты: {document['blocking_accepted']}.",
        "",
        "| Сценарий / вариант | Статус | HTTP МИС | POST визита | PATCH визита | Время, мс | Причины |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for item in document["results"]:
        reasons = ", ".join(sorted({finding["code"] for finding in item["findings"]})) or "—"
        label = item["label"].replace("|", "/")
        lines.append(
            f"| {label} | {item['status']} | {item['requests']['total']} | "
            f"{item['mutations']['visits_created']} | {item['mutations']['visits_patched']} | "
            f"{item['elapsed_ms']} | {reasons} |"
        )
    lines += [
        "",
        "Время — исполнение в памяти на этой машине, без задержки телефонной сети.",
        "Профиль functional_unlimited_emulator не является доказательством соблюдения квоты.",
        "unsupported означает непроверенное требование; fail — наблюдаемое несовпадение.",
        "Полный журнал HTTP без заголовков, тел и персональных фильтров находится в соседнем JSON.",
        "",
    ]
    lines += [
        "## Запросы С-01 / С-03 / С-04 / С-05 / С-10",
        "",
        "| Диалог | Статус | Профиль | GET | POST | PATCH | Всего |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for item in document["request_scenarios"]:
        counts = item["requests"]["by_method_resource"]
        by_method = {
            method: sum(v for k, v in counts.items() if k.startswith(method + " "))
            for method in ("GET", "POST", "PATCH")
        }
        lines.append(
            f"| {item['label']} | {item['status']} | {item['budget_profile']} | "
            f"{by_method['GET']} | {by_method['POST']} | {by_method['PATCH']} | "
            f"{item['requests']['total']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require", choices=("all", "blocking"), default="all")
    args = parser.parse_args(argv)
    results = asyncio.run(execute_suite())
    document = report(results)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "results.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (args.output_dir / "results.md").write_text(markdown(document), encoding="utf-8")
    print(
        json.dumps(
            {
                "counts": document["counts"],
                "blocking_accepted": document["blocking_accepted"],
                "all_accepted": document["all_accepted"],
            },
            ensure_ascii=False,
        )
    )
    if any(f.kind == "error" for r in results for f in r.findings):
        return 2
    return 0 if accepted(results, args.require) else 1


if __name__ == "__main__":
    raise SystemExit(main())
