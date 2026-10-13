"""Где ошибается распознавание: фразы, на которых разбор дал не тот результат, что из эталона.

python bench/speech/errors.py [движок] [условие]
"""

import json
import sys
from collections import Counter

from bench import PHRASES, RESULTS, check_item

LABELS = {
    "slots_ok": "параметры", "intent_ok": "ответ да/нет", "flags_ok": "флаг не найден",
    "digits_ok": "номер полиса", "false_alarm": "ложная тревога",
}


def failures(engine: str, condition: str) -> list[tuple[str, str, str, list[str]]]:
    data = json.loads((RESULTS / f"asr_{engine}.json").read_text(encoding="utf-8"))
    items = {i["id"]: i for i in PHRASES["patient"]}
    found = []
    for row in data["rows"]:
        if row["condition"] != condition:
            continue
        result = check_item(items[row["id"]], row["text"])
        bad = [k for k, ok in result.items() if k != "false_alarm" and not ok] + (["false_alarm"] if result["false_alarm"] else [])
        if bad:
            found.append((row["id"], row["voice"], row["text"], bad))
    return found


def markdown(engine: str, condition: str, limit: int = 12) -> str:
    rows = failures(engine, condition)
    items = {i["id"]: i["text"] for i in PHRASES["patient"]}
    by_phrase = Counter(r[0] for r in rows)
    lines = [f"**{engine}, {condition}:** ошибки разбора в {len(rows)} распознаваниях.", "",
             "| Фраза | Сколько голосов | Пример распознавания | Что не так |", "|---|---|---|---|"]
    seen = set()
    for phrase_id, _ in by_phrase.most_common():
        if len(seen) >= limit:
            break
        example = next(r for r in rows if r[0] == phrase_id)
        seen.add(phrase_id)
        problems = ", ".join(LABELS.get(k, k) for k in example[3])
        lines.append(f"| {items[phrase_id]} | {by_phrase[phrase_id]} | {example[2]} | {problems} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(sys.argv[1] if len(sys.argv) > 1 else "gigaam_v3_ctc", sys.argv[2] if len(sys.argv) > 2 else "phone"))
